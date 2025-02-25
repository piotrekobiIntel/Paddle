# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License

import copy
import numpy as np
import paddle
import paddle.fluid as fluid
from paddle.fluid import core
from paddle.fluid import framework as framework
from paddle.fluid import core, unique_name
from paddle.fluid.framework import Program, Parameter, Variable, program_guard
from paddle.fluid.data_feeder import check_variable_and_dtype, check_dtype
from paddle.fluid.backward import append_backward, _some_in_set_, _append_grad_suffix_
from paddle.distributed.auto_parallel.operators.common import get_distributed_operator
from paddle.fluid.clip import GradientClipBase, GradientClipByNorm, error_clip_callback, append_gradient_clip_ops, ClipGradByGlobalNorm
from paddle.distributed.fleet.base.distributed_strategy import DistributedStrategy
from paddle.distributed.auto_parallel.context import DistributedContext, DistOpHelper
from paddle.distributed.fleet.meta_optimizers.common import is_loss_grad_op, is_backward_op, is_optimizer_op
from paddle.distributed.fleet.meta_optimizers.common import OpRole, OP_ROLE_KEY, OP_ROLE_VAR_KEY
from .process import new_process_group
from .interface import _g_process_mesh_map
from .attribute import OperatorDistributedAttribute
from paddle.distributed.auto_parallel.completion import complete_backward_annotation, complete_update_annotation

__varname_not_in_block__ = ["lod_tensor_blocking_queue_0"]


class Partitioner(object):
    """
    warning:: Partitioner is experimental and subject to change.

    Partitioner convert a program into another program.
    Given a serial program which has been auto completed with shard annotation, the Partitioner 
    convert the serial program into a "distributed" program. The Partitioner will  modify the serial
    program in following two ways, which is also the major difference between serial and distributed program:
        1. partition op: replace a serial op into its corresponding dist op infered from the shard annotation
        2. partition var: if a var is sharded, modify the shape of var according to its shard annotation

    Partitioner is supposed to be call by the auto parallel framework, and not supposed to be directly called by user.

    Example:
        ....
            import paddle.distributed.auto_parallel as auto
            from paddle.fluid.distributed_attribute import get_default_distributed_context
            from paddle.distributed import fleet
            from paddle.distributed.auto_parallel.partitioner import Partitioner

            # create serial program with forward only 
            with static.program_guard(serial_main_program, serial_start_program):
                model = create_model(config)
                tokens = static.data(name="tokens", shape=[batch_size, sequence_len], dtype='int64')
                labels = static.data(name="labels", shape=[batch_size, sequence_len], dtype='int64')
                loss_mask = static.data(name="loss_mask", shape=[batch_size, sequence_len], dtype='int64')
                preds = model(tokens)
                loss = criterion(preds, labels, loss_mask)

            # auto completion
            auto.ProcessMesh(shape=[2, 4], process_group=[0, 1, 2, 3, 4, 5, 6, 7])
            annotated_main_program = auto.complete_annotation(serial_main_program)
            auto_paralle_context = get_default_distributed_context()
                
            # distributed strategy & rank info
            rank_id = paddle.distributed.get_rank()
            dist_strategy = fleet.DistributedStrategy()
    
            # create partitioner
            Partitioner = Partitioner(dist_strategy, auto_paralle_context, rank_id)

            # create dist program with forward only
            # for distributed inference, using partitioned_main_prog from here
            partitioned_main_prog, partitioned_startup_prog = Partitioner.transpile_forward(complete_train_program, start_program)

            # create dist program with forward/backward/update
            # for distributed training, using partitioned_main_prog from here
            dist_params_grads = Partitioner.apply_backward(loss, complete_train_program, start_program, partitioned_main_prog, partitioned_startup_prog)
            optimizer = paddle.fluid.optimizer.AdamOptimizer(
                learning_rate=0.00001,
                beta1=0.9,
                beta2=0.999,
                epsilon=1e-08,
                grad_clip=None)
            opt_ops = Partitioner.apply_optimize(optimizer, dist_params_grads, partitioned_main_prog, partitioned_startup_prog)
    """

    def __init__(self, dist_strategy, auto_parallel_context, rank_id=0):
        """
        Args:
            dist_strategy (paddle.fleet.distributed_strategy): used to determine the user defined distributed strategy.
            auto_parallel_context (paddle.fluid.DistributedContext): used to access the distributed_attr of var & op, every Partitioner object could maintain its own DistributedContext member, and partition program base on that shard scenario.
            rank_id (int): global rank id to which the partitioned distributed program belong.
        """

        if not isinstance(dist_strategy, DistributedStrategy):
            raise TypeError(
                "dist_strategy be paddle.fleet.base.DistributedStrategy, got %s here"
                % type(dist_strategy))

        if not isinstance(auto_parallel_context, DistributedContext):
            raise TypeError(
                "auto_parallel_context be paddle.fluid.DistributedContext, got %s here"
                % type(auto_parallel_context))

        self._dist_strategy = dist_strategy
        self._auto_parallel_context = auto_parallel_context
        self._rank_id = rank_id
        self._serial2dist_varname_mapping = {}
        self._dist_varname_suffix = ""

        # TODO if there is some dist op that is not compatible 
        # with auto_backward in forward, the following flag 
        # should be set to False
        self._compatible_with_auto_backward = True

    def transpile_forward(self, serial_main_program, serial_startup_program):
        """
        take serial forward programs with shard annotation, create a new distributed forward programs based on the serial ones.
        instead of modify the input programs inplace, this function will preserve the inputs and create new program for output.

        beside replace the serial op with its dist op, if user has defined other strategy in fleet.distributed_strategy, and if 
        those strategy need to transpile (modify) the forward network program, those forward program modification should also be done within this
        function in auto parallel scenario, in order to facilitate distributed inference/evaluation which need to DECOUPLE strategy specific forward transpilation with fleet.distributed_optimizer.minimize().

        by now the fleet.distributed_strategy that need transpile forward program are following: 
            1. (optimizer) sharding

        Args:
            main_program (paddle.fluid.framework.program): serial main program with forward network only
            startup_program (paddle.fluid.framework.program): serial startup program with forward network only
        
        return:
            main_program (paddle.fluid.framework.program): distributed main program with forward network only
            startup_program (paddle.fluid.framework.program): distributed startup program with forward network only
        """

        dist_main_program, dist_startup_program = self.transpile_forward_impl(
            serial_main_program, serial_startup_program)
        return dist_main_program, dist_startup_program

    def apply_backward(self,
                       serial_loss,
                       serial_main_program,
                       serial_startup_program,
                       dist_main_program,
                       dist_startup_program,
                       parameter_list=None,
                       no_grad_set=None,
                       callbacks=None):
        """
        A complete training neural network is made up of forward and backward propagation. 
        This function is to generate the dist backward program for the distributed forward program.

        By now, the current automatical backward mechanism in paddle framework might NOT handle the backward generation for 
        some dist ops correctly, some so we now have two ways to genenate the backward program:
            1. dist_forward_program --> auto_backward --> dist_backward_program (if auto_backward could handle all dist op)
            2. serial_forward_program --> auto_backward --> serial_backward_program --> dist_op_backward_transpile --> dist_backward_program (if auto_backward could not handle all dist op)
        
        the backprogram is append the input dist program inplaced.

        Args:
            serial_loss (Variable) the loss in serial program that to be minimized 
            serial_main_program (paddle.fluid.framework.program): serial main program with forward network only
            serial_startup_program (paddle.fluid.framework.program): serial startup program with forward network only
            dist_main_program (paddle.fluid.framework.program): dist main program with forward network only
            dist_startup_program (paddle.fluid.framework.program): dist startup program with forward network only
            parameter_list (Iterable, optional): Iterable of ``Variable`` or ``Variable.name`` to update
                to minimize ``loss``. The default value is None, at this time all parameters
                will be updated.
            no_grad_set (set, optional): Set of ``Variable``  or ``Variable.name`` that don't need
                to be updated. The default value is None.
            callbacks (list, optional): list of callable objects to run when appending backward
                operator for one parameter. The default value is None.
        
        return:
            params_grads (list) list of tuple that contain param and its grad variable
        """
        params_grads = self.apply_backward_impl(
            serial_loss, serial_main_program, serial_startup_program,
            dist_main_program, dist_startup_program)
        return params_grads

    def apply_optimize(self, user_define_optimizer, params_grads,
                       dist_main_program, dist_startup_program):
        """
        append update related ops to the program: clip, weight decay, ops
        filter optimize op if sharding is enable
        naive gradient synchronization before update

        Args:
            user_define_optimizer (paddle.fluid.optimizer): 
            params_grads (list) list of tuple that contain param and its grad variable
            dist_main_program (paddle.fluid.framework.program): dist main program with forward & backward network 
            dist_startup_program (paddle.fluid.framework.program): dist startup program with forward & backward  network 
        """

        optimize_ops = self.apply_optimize_impl(user_define_optimizer,
                                                params_grads, dist_main_program,
                                                dist_startup_program)

        return optimize_ops

    def transpile_forward_impl(self, main_program, startup_program):

        if not isinstance(main_program, (Program)):
            raise TypeError(
                "dist_strategy be paddle.fluid.framework.program, got %s here" %
                type(main_program))

        if not isinstance(startup_program, (Program)):
            raise TypeError(
                "auto_parallel_context be paddle.fluid.framework.program, got %s here"
                % type(startup_program))

        # check if shard annotated serial program valid
        if not self._is_valid_annotated_program(main_program):
            raise RuntimeError(
                "Not all vars or ops are annotated in main program !")

        # dist op & partition vars
        new_main_prog, new_startup_program = self._dist_var_op_forward_transpile(
            main_program, startup_program)

        # Sharding
        if self._dist_strategy.sharding:
            new_main_prog, new_startup_program = self._sharding_forward_transpile(
                new_main_prog, new_startup_program)

        return new_main_prog, new_startup_program

    def apply_backward_impl(self,
                            serial_loss,
                            serial_main_program,
                            serial_startup_program,
                            dist_main_program,
                            dist_startup_program,
                            parameter_list=None,
                            no_grad_set=None,
                            callbacks=None):
        """
        """

        params_grads = self._dist_var_op_backward_transpile(
            serial_loss, serial_main_program, serial_startup_program,
            dist_main_program, dist_startup_program)
        # Sharding
        if self._dist_strategy.sharding:
            self._sharding_backward_transpile(new_main_prog,
                                              new_startup_program)

        return params_grads

    def apply_optimize_impl(self, user_define_optimizer, params_grads,
                            dist_main_program, dist_startup_program):
        """
        append update related ops to the program: clip, weight decay, ops
        filter optimize op if sharding is enable
        naive gradient synchronization before update

        Args:
            user_define_optimizer (paddle.fluid.optimizer): 
            params_grads (list) list of tuple that contain param and its grad variable
            dist_main_program (paddle.fluid.framework.program): dist main program with forward & backward network 
            dist_startup_program (paddle.fluid.framework.program): dist startup program with forward & backward  network 
        """

        if self._dist_strategy.sharding:
            params_grads = sharding_optimize_transpile(
                params_grads, dist_main_program, dist_startup_program)

        optimize_ops = self._optimize_transpile(user_define_optimizer,
                                                params_grads, dist_main_program,
                                                dist_startup_program)

        return optimize_ops

    def _dist_var_op_forward_transpile(self,
                                       serial_main_program,
                                       serial_startup_program=None):
        """
        1. partition variables
        2. replace local op with corresponding dist op
        """

        partitioned_main_prog = fluid.Program()
        partitioned_global_block = partitioned_main_prog.global_block()
        serial_main_block = serial_main_program.global_block()
        serial_ops = serial_main_program.global_block().ops

        # transpile startup program
        if serial_startup_program == None:
            partitioned_startup_prog = None
        else:
            partitioned_startup_prog = fluid.Program()
            # create parameter
            partitioned_startup_global_block = partitioned_startup_prog.global_block(
            )
            param2shape = {}
            temp_varname_map = {}
            for var in serial_startup_program.list_vars():
                if isinstance(var, Parameter):
                    # TODO if var not belong to this rank, should be filtered
                    serial_main_var = serial_main_block.var(var.name)
                    dist_attr = self._auto_parallel_context.get_tensor_distributed_attr_for_program(
                        serial_main_var)
                    target_shape = _get_dist_shape(serial_main_var, dist_attr)
                    new_name = var.name + self._dist_varname_suffix
                    temp_varname_map[var.name] = new_name
                    _partition_parameter(self._auto_parallel_context,
                                         serial_main_var,
                                         partitioned_startup_global_block,
                                         new_name, target_shape)
                    param2shape[new_name] = target_shape

            # copy initializer
            for op in serial_startup_program.global_block().ops:
                # TODO if var not belong to this rank, should be filtered
                output_vars = op.desc.output_arg_names()
                assert len(
                    output_vars
                ) == 1, "initializer should output only ONE variable, but got [{}]".format(
                    str(op.desc))
                assert temp_varname_map[output_vars[
                    0]] in param2shape, "try to initialize [{}] which is not a Parameter".format(
                        output_vars[0])
                new_op_desc = partitioned_startup_global_block.desc.append_op()
                new_op_desc.copy_from(op.desc)
                new_op_desc._rename_output(output_vars[0],
                                           temp_varname_map[output_vars[0]])
                new_op_desc._set_attr(
                    "shape", param2shape[temp_varname_map[output_vars[0]]])
                partitioned_startup_global_block._sync_with_cpp()

                # set distribute atrribute
                new_op = partitioned_startup_global_block.ops[-1]
                assert new_op.type == new_op_desc.type()
                assert new_op.desc == new_op_desc
                output_var = partitioned_startup_global_block.var(output_vars[
                    0])
                output_var_attr = self._auto_parallel_context.get_tensor_distributed_attr_for_program(
                    output_var)
                op_attr = OperatorDistributedAttribute(
                    new_op, self._auto_parallel_context)
                op_attr.set_process_mesh(output_var_attr.get_process_mesh())
                op_attr.set_output_dims_mapping(
                    output_var.name, output_var_attr.get_dims_mapping())
                op_attr.set_input_dims_mapping(
                    output_var.name, output_var_attr.get_dims_mapping())
                self._auto_parallel_context.set_op_distributed_attr_for_program(
                    new_op, op_attr)

        # TODO move helper init to a comm place
        dist_op_helper = self._auto_parallel_context.get_dist_op_helper()
        dist_op_helper.set_dst_main_program(partitioned_main_prog)
        dist_op_helper.set_dst_startup_program(partitioned_startup_prog)
        dist_op_helper.set_varname_mapping(self._serial2dist_varname_mapping)
        dist_op_helper.set_rank_id(self._rank_id)

        # transpile main program
        for op in serial_ops:

            # partititon input variables
            for serial_input_varname in op.desc.input_arg_names():
                if serial_input_varname not in self._serial2dist_varname_mapping:
                    new_varname = serial_input_varname + self._dist_varname_suffix
                    if serial_main_block.has_var(serial_input_varname):
                        _partition_var(self._auto_parallel_context,
                                       serial_main_block,
                                       partitioned_global_block,
                                       serial_input_varname, new_varname)
                    else:
                        assert serial_input_varname in __varname_not_in_block__

                    self._serial2dist_varname_mapping[
                        serial_input_varname] = new_varname

            # partition output vars
            for serial_output_varname in op.desc.output_arg_names():
                if serial_output_varname not in self._serial2dist_varname_mapping:
                    new_varname = serial_output_varname + self._dist_varname_suffix
                    _partition_var(self._auto_parallel_context,
                                   serial_main_block, partitioned_global_block,
                                   serial_output_varname, new_varname)
                    self._serial2dist_varname_mapping[
                        serial_output_varname] = new_varname

            # partition op
            kinputs, koutputs = dist_op_helper.prepare_forward_context(op)
            dist_attr = self._auto_parallel_context.get_op_distributed_attr_for_program(
                op)
            if _is_dist_op_forward_implement(self._auto_parallel_context, op):
                dist_ops = get_distributed_operator(op.type)
                dist_op_impl = dist_ops.get_impl(dist_attr.get_impl_idx())
                dist_op_impl.forward(self._auto_parallel_context, **kinputs,
                                     **koutputs)

            else:
                # replicate op
                dist_ops = get_distributed_operator("default")
                dist_op_impl = dist_ops.get_impl(0)
                dist_op_impl.forward(self._auto_parallel_context, **kinputs,
                                     **koutputs)

        return partitioned_main_prog, partitioned_startup_prog

    def _dist_var_op_backward_transpile(self,
                                        serial_loss,
                                        serial_main_program,
                                        serial_startup_program,
                                        dist_main_program,
                                        dist_startup_program,
                                        parameter_list=None,
                                        no_grad_set=None,
                                        callbacks=None):
        """
        so far, the auto_backward case only guarantee the correcotness of backward ops for curtain Dist ops:
            1. NV-Megatron-like parallel embedding
            2. NV-Megatron-like row parallel linear
            3. NV-Megatron-like col parallel linear
        """

        if self._compatible_with_auto_backward:
            assert isinstance(
                serial_loss, Variable), "The target loss should be an Variable."
            dist_loss = self._serial_varname2dist_var(serial_loss.name,
                                                      dist_main_program)

            assert len(dist_loss.shape) == 1 and dist_loss.shape[0] == 1, \
                "The dist loss.shape should be (1L,), but the current dist loss.shape is {}. " \
                "Maybe that you should call fluid.layers.mean to process the current loss.".format(
                    dist_loss.shape)

            # update parameter list
            if parameter_list:
                parameter_list = [
                    self._serial_varname2dist_var(param.name, dist_main_program)
                    for param in parameter_list
                ]

            # update parameter no_grad_set
            if no_grad_set:
                no_grad_set = [
                    self._serial_varname2dist_var(param.name, dist_main_program)
                    for param in no_grad_set
                ]

            dist_op_helper = self._auto_parallel_context.get_dist_op_helper()
            params_and_grads = _auto_backward(
                dist_loss,
                dist_startup_program,
                parameter_list=parameter_list,
                no_grad_set=no_grad_set,
                callbacks=callbacks,
                distop_context=dist_op_helper)

            # backward completion 
            complete_backward_annotation(
                dist_main_program, dist_context=self._auto_parallel_context)

            # transpiler backward for dist op
            # get backward ops
            ops = dist_main_program.global_block().ops
            first_backward_op_idx = -1
            forward_op_id2forward_op = {}
            for idx in range(len(ops)):
                if is_forward_op(ops[idx]):
                    forward_op_id2forward_op[ops[idx].desc.id()] = ops[idx]

                if int(ops[idx].attr('op_role')) == int(OpRole.Backward):
                    first_backward_op_idx = idx
                    break
            assert first_backward_op_idx >= 0, "not found backward ops in program"
            assert len(forward_op_id2forward_op
                       ) > 0, "not found forward ops in program"

            backward_ops = ops[first_backward_op_idx:]
            for backward_op in backward_ops:
                # if the backward op has a corresponding forward op
                if backward_op.desc.id() in dist_op_helper.gradopidx2opidx:
                    forward_op_id = dist_op_helper.gradopidx2opidx[
                        backward_op.desc.id()]
                    forward_op = forward_op_id2forward_op[forward_op_id]
                    # TODO backward attr should has _impl_idx
                    forward_op_dist_attr = self._auto_parallel_context.get_op_distributed_attr_for_program(
                        forward_op)
                    # TODO use the backward op itself to find the dist op
                    dist_ops = get_distributed_operator(forward_op.type)
                    kinputs, koutputs = dist_op_helper.prepare_backward_context(
                        backward_op)

                    # TODO use backward op itself to determine impl idx
                    if _is_dist_op_backward_implement(
                            self._auto_parallel_context, forward_op):
                        dist_op_impl = dist_ops.get_impl(
                            forward_op_dist_attr.get_impl_idx())
                        dist_op_impl.backward(self._auto_parallel_context,
                                              **kinputs, **koutputs)
                    else:
                        # replicate op
                        dist_ops = get_distributed_operator("default")
                        dist_op_impl = dist_ops.get_impl(0)
                        dist_op_impl.backward(self._auto_parallel_context,
                                              **kinputs, **koutputs)

            return params_and_grads
        # replace dist grad ops
        else:
            raise RuntimeError("transpile NOT implemented !")

    def _optimize_transpile(self, user_define_optimizer, params_grads,
                            main_program, startup_program):

        with program_guard(main_program, startup_program):
            optimize_ops = user_define_optimizer.apply_gradients(params_grads)

        # update completion 
        complete_update_annotation(
            main_program, dist_context=self._auto_parallel_context)

        return optimize_ops

    def _is_valid_annotated_program(self, program):

        # TODO (ZJ-LIANG) should check all block
        ops = program.global_block().ops
        vars_ = program.list_vars()
        op_dist_attrs = [
            self._auto_parallel_context.get_op_distributed_attr_for_program(op)
            for op in ops
        ]
        var_dist_attrs = [
            self._auto_parallel_context.get_tensor_distributed_attr_for_program(
                var) for var in vars_
        ]

        all_ops_annotated = all(dist_attr is not None
                                for dist_attr in op_dist_attrs)
        all_vars_annotated = all(dist_attr is not None
                                 for dist_attr in var_dist_attrs)

        return all_ops_annotated and all_vars_annotated

    def _serial_varname2dist_var(self, serial_varname, dist_program):
        assert serial_varname in self._serial2dist_varname_mapping, "The serial var [{}] is not found in var name mapping".format(
            serial_varname)
        dist_varname = self._serial2dist_varname_mapping[serial_varname]

        assert dist_program.global_block().has_var(
            dist_varname
        ), "The dist var [{}] is not found in dist program".format(dist_varname)
        dist_var = dist_program.global_block().var(dist_varname)

        return dist_var

    def _is_var_distributed(self, var):

        dist_attr = self._auto_parallel_context.get_tensor_distributed_attr_for_program(
            var)
        assert dist_attr is not None, "dist_attr of var [{}] is None".format(
            var.name)
        return _is_distributed(dist_attr)

    def _sharding_forward_transpile(self, main_prog, startup_program):
        """
        this transpile conduct the modification in forward program need by sharding strategy
        which majorly include:
            1. partition the parameter
            2. insert broadcast op
            3. insert sync op 

        NOTE the transpile modification is inplace on the input program
        """

        raise NotImplementedError(
            "Sharding is NOT support in AutoParallel yet!")

    def _sharding_backward_transpile(self, main_prog, startup_program):
        """
        this transpile conduct the modification in backward program need by sharding strategy
        which majorly include:
            1. partition the gradient
            2. insert broadcast op
            3. insert sync op 

        NOTE the transpile modification is inplace on the input program
        """

        raise NotImplementedError(
            "Sharding is NOT support in AutoParallel yet!")

    def _sharding_optimize_transpile(self, params_grads, dist_main_program,
                                     dist_startup_program):
        """
        shard params_grads
        append the broadcast to sync parameters 
        """
        raise RuntimeError("sharding transpile is NOT implemented !")


def _get_no_grad_set_name(no_grad_set):
    no_grad_set_name = set()
    if no_grad_set is not None:
        if isinstance(no_grad_set, (set, list, tuple)):
            for i, no_grad_var in enumerate(no_grad_set):
                if isinstance(no_grad_var, framework.Variable):
                    no_grad_set_name.add(no_grad_var.name)
                elif isinstance(no_grad_var, six.string_types):
                    no_grad_set_name.add(no_grad_var)
                else:
                    raise TypeError(
                        "The type of no_grad_set's member must be paddle.fluid.Variable or str, but received %s."
                        % (type(no_grad_var)))
        else:
            raise TypeError(
                "The type of no_grad_set should be set or list or tuple, but received {}".
                format(type(no_grad_set)))
    return no_grad_set_name


def _get_no_grad_set(loss, no_grad_set=None):
    no_grad_set = _get_no_grad_set_name(no_grad_set)
    parameters = loss.block.program.global_block().all_parameters()
    param_no_trainable = set(
        [param.name for param in parameters if param.trainable is False])
    # If the parameter is no trainable, it should not have a gradient.
    no_grad_set.update(param_no_trainable)

    return no_grad_set


def _is_dist_op_forward_implement(auto_paralle_context, op):
    dist_attr = auto_paralle_context.get_op_distributed_attr_for_program(op)
    dist_ops = get_distributed_operator(op.type)

    return dist_ops and dist_attr.get_impl_idx() >= 0 and dist_ops.get_impl( \
        dist_attr.get_impl_idx())._forward_implemented


def _is_dist_op_backward_implement(auto_paralle_context, op):
    dist_attr = auto_paralle_context.get_op_distributed_attr_for_program(op)
    dist_ops = get_distributed_operator(op.type)

    return dist_ops and dist_attr.get_impl_idx() >= 0 and dist_ops.get_impl( \
        dist_attr.get_impl_idx())._backward_implemented


def _auto_backward(loss,
                   startup_program=None,
                   parameter_list=None,
                   no_grad_set=None,
                   callbacks=None,
                   distop_context=None):
    """
    modification is inplaced
    """
    act_no_grad_set = _get_no_grad_set(loss, no_grad_set)
    assert isinstance(loss, Variable), "The target loss should be an Variable."

    if callbacks is None:
        callbacks = [error_clip_callback]
    else:
        assert (isinstance(callbacks, list))

    assert len(loss.shape) == 1 and loss.shape[0] == 1, \
        "The loss.shape should be (1L,), but the current loss.shape is {}. " \
        "Maybe that you should call fluid.layers.mean to process the current loss.".format(
            loss.shape)

    program = loss.block.program

    with program_guard(program, startup_program):
        params_grads = append_backward(
            loss,
            parameter_list,
            act_no_grad_set,
            callbacks,
            distop_context=distop_context)

    return params_grads


def _is_distributed(dist_attr):

    mapping = dist_attr.get_dims_mapping()
    mesh = dist_attr.get_process_mesh().topology
    for idx in range(len(mapping)):
        if mapping[idx] >= 0 and mesh[mapping[idx]] > 1:
            return True

    return False


def _get_dist_shape(var, dist_attr):

    var_shape = var.shape
    mapping = dist_attr.get_dims_mapping()
    mesh = dist_attr.get_process_mesh().topology
    assert len(var_shape) == len(
        mapping
    ), "variable shape [{}] and dim_mapping [{}] is NOT match !".format(
        var_shape, mapping)
    new_shape = []
    for idx in range(len(var_shape)):
        if var_shape[idx] == -1 or mapping[idx] == -1:
            new_shape.append(var_shape[idx])
        else:
            assert var_shape[idx] % mesh[mapping[
                idx]] == 0, "un-event partition: var_shape[idx]=[{}], mesh[{}]".format(
                    var_shape[idx], mesh[mapping[idx]])
            new_shape.append(var_shape[idx] // mesh[mapping[idx]])

    return new_shape


def _partition_parameter(auto_paralle_context, src_var, dst_block, dst_varname,
                         dst_shape):
    # NOTE hack to copied Parameter
    # not initialized parameter, need to initialize it 
    copied_kwargs = {}
    copied_kwargs['trainable'] = src_var.trainable
    copied_kwargs['optimize_attr'] = src_var.optimize_attr
    copied_kwargs['regularizer'] = src_var.regularizer
    copied_kwargs['do_model_average'] = src_var.do_model_average
    copied_kwargs['need_clip'] = src_var.need_clip

    param = Parameter(
        block=dst_block,
        type=src_var.type,
        name=dst_varname,
        shape=dst_shape,
        dtype=src_var.dtype,
        lod_level=src_var.lod_level,
        error_clip=src_var.error_clip,
        stop_gradient=src_var.stop_gradient,
        is_data=src_var.is_data,
        belong_to_optimizer=src_var.belong_to_optimizer,
        **copied_kwargs)

    # set dist attr uid
    # distributed_attr_uid = src_var.desc.get_distributed_attr_uid()
    # param.desc.set_distributed_attr_uid(distributed_attr_uid)
    dist_attr = copy.deepcopy(
        auto_paralle_context.get_tensor_distributed_attr_for_program(src_var))
    assert dist_attr is not None
    dist_attr._owner_tensor = param
    dist_attr._owner_context = auto_paralle_context.get_tensor_distributed_attr_for_program(
        src_var)._owner_context
    auto_paralle_context.set_tensor_distributed_attr_for_program(param,
                                                                 dist_attr)


def _partition_intermediate_var(auto_paralle_context, src_var, dst_block,
                                dst_varname, dst_shape):
    var = dst_block.create_var(
        type=src_var.type,
        name=dst_varname,
        shape=dst_shape,
        dtype=src_var.dtype,
        lod_level=src_var.lod_level,
        persistable=src_var.persistable,
        error_clip=src_var.error_clip,
        stop_gradient=src_var.stop_gradient,
        is_data=src_var.is_data,
        belong_to_optimizer=src_var.belong_to_optimizer)

    # set dist attr uid
    # distributed_attr_uid = src_var.desc.get_distributed_attr_uid()
    # var.desc.set_distributed_attr_uid(distributed_attr_uid)
    dist_attr = copy.deepcopy(
        auto_paralle_context.get_tensor_distributed_attr_for_program(src_var))
    assert dist_attr is not None
    dist_attr._owner_tensor = var
    dist_attr._owner_context = auto_paralle_context.get_tensor_distributed_attr_for_program(
        src_var)._owner_context
    auto_paralle_context.set_tensor_distributed_attr_for_program(var, dist_attr)


def _partition_var(auto_paralle_context, src_block, dst_block, src_varname,
                   dst_varname):
    """
    partition include: split + replicate
    """
    src_var = src_block.var(src_varname)

    if src_var.type == core.VarDesc.VarType.READER:
        dst_block.create_var(
            type=src_var.type,
            name=dst_varname,
            persistable=True,
            stop_gradient=True)
    else:
        dist_attr = auto_paralle_context.get_tensor_distributed_attr_for_program(
            src_var)
        target_shape = _get_dist_shape(src_var, dist_attr)

        if isinstance(src_var, Parameter):
            _partition_parameter(auto_paralle_context, src_var, dst_block,
                                 dst_varname, target_shape)
        else:
            _partition_intermediate_var(auto_paralle_context, src_var,
                                        dst_block, dst_varname, target_shape)


def _insert_src_op(src_op, dst_block, varname_mapping):

    new_op_desc = dst_block.desc.append_op()
    new_op_desc.copy_from(src_op.desc)
    for local_varname in src_op.desc.input_arg_names():
        new_op_desc._rename_input(local_varname, varname_mapping[local_varname])
    for local_varname in src_op.desc.output_arg_names():
        new_op_desc._rename_output(local_varname,
                                   varname_mapping[local_varname])
    dst_block._sync_with_cpp()


def _insert_dist_op(src_op, dst_block, varname_mapping, auto_paralle_context,
                    rank_id):

    # build input varname mapping
    input_mapping = {}
    for input_name in src_op.desc.input_names():
        varnames = []
        for varname in src_op.desc.input(input_name):
            varnames.append(varname_mapping[varname])
        input_mapping[input_name] = varnames

    # build output varname mapping
    output_mapping = {}
    for output_name in src_op.desc.output_names():
        varnames = []
        for varname in src_op.desc.output(output_name):
            varnames.append(varname_mapping[varname])
        output_mapping[output_name] = varnames

    # append dist op 
    dist_attr = auto_paralle_context.get_op_distributed_attr_for_program(src_op)
    dist_ops = get_distributed_operator(src_op.type)
    append_op_handle = dist_ops.get_impl(dist_attr.get_impl_idx()).forward(
        src_op)
    append_op_handle(
        dst_block,
        src_op,
        dist_attr,
        input_mapping,
        output_mapping,
        rank_id=rank_id)


def is_forward_op(op):
    role1 = int(core.op_proto_and_checker_maker.OpRole.Forward) | int(
        core.op_proto_and_checker_maker.OpRole.Loss)
    role2 = int(core.op_proto_and_checker_maker.OpRole.Forward)
    op_role = int(op.attr('op_role'))
    return op_role == role2 or op_role == role1
