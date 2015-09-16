"""
This is the core of the vec. optimization. It combines dependency.py and schedule.py
to rewrite a loop in vectorized form.

See the rpython doc for more high level details.
"""

import py
import time

from rpython.jit.metainterp.resume import Snapshot
from rpython.jit.metainterp.jitexc import NotAVectorizeableLoop, NotAProfitableLoop
#from rpython.jit.metainterp.optimizeopt.unroll import optimize_unroll
from rpython.jit.metainterp.compile import (ResumeAtLoopHeaderDescr,
        CompileLoopVersionDescr, invent_fail_descr_for_op, ResumeGuardDescr)
from rpython.jit.metainterp.history import (INT, FLOAT, VECTOR, ConstInt, ConstFloat,
        TargetToken, JitCellToken, AbstractFailDescr)
from rpython.jit.metainterp.optimizeopt.optimizer import Optimizer, Optimization
from rpython.jit.metainterp.optimizeopt.renamer import Renamer
from rpython.jit.metainterp.optimizeopt.dependency import (DependencyGraph,
        MemoryRef, Node, IndexVar)
from rpython.jit.metainterp.optimizeopt.version import LoopVersionInfo
from rpython.jit.metainterp.optimizeopt.schedule import (VecScheduleState,
        Scheduler, Pack, Pair, AccumPair)
from rpython.jit.metainterp.optimizeopt.guard import GuardStrengthenOpt
from rpython.jit.metainterp.resoperation import (rop, ResOperation, GuardResOp, Accum)
from rpython.rlib import listsort
from rpython.rlib.objectmodel import we_are_translated
from rpython.rlib.debug import debug_print, debug_start, debug_stop
from rpython.rlib.jit import Counters
from rpython.rtyper.lltypesystem import lltype, rffi

class VectorLoop(object):
    def __init__(self, label, oplist, jump):
        self.label = label
        self.inputargs = label.getarglist()
        self.prefix = []
        self.prefix_label = None
        assert self.label.getopnum() == rop.LABEL
        self.operations = oplist
        self.jump = jump
        assert self.jump.getopnum() == rop.JUMP

    def operation_list(self):
        return [self.label] + self.operations + [self.jump]

    def assemble_oplist(self):
        oplist = self.prefix + [self.prefix_label] + \
                 loop.operations + [loop.jump]
        return oplist

def optimize_vector(metainterp_sd, jitdriver_sd, warmstate, loop_info, loop_ops):
    """ Enter the world of SIMD. Bails if it cannot transform the trace. """
    user_code = not jitdriver_sd.vec and warmstate.vec_all
    if user_code and user_loop_bail_fast_path(loop, warmstate):
        return
    # the original loop (output of optimize_unroll)
    info = LoopVersionInfo(loop_info)
    version = info.snapshot(loop_ops, info.label_op)
    loop = VectorLoop(loop_info.label_op, loop_ops[:-1], loop_ops[-1])
    try:
        debug_start("vec-opt-loop")
        metainterp_sd.logger_noopt.log_loop([], loop.operation_list(), -2, None, None, "pre vectorize")
        metainterp_sd.profiler.count(Counters.OPT_VECTORIZE_TRY)
        #
        start = time.clock()
        opt = VectorizingOptimizer(metainterp_sd, jitdriver_sd, warmstate.vec_cost)
        opt.propagate_all_forward(info, loop)
        #
        gso = GuardStrengthenOpt(opt.dependency_graph.index_vars, opt.has_two_labels)
        gso.propagate_all_forward(info, loop, user_code)
        end = time.clock()
        #
        metainterp_sd.profiler.count(Counters.OPT_VECTORIZED)
        metainterp_sd.logger_noopt.log_loop([], loop.operation_list(), -2, None, None, "post vectorize")
        #
        nano = int((end-start)*10.0**9)
        debug_print("# vecopt factor: %d opcount: (%d -> %d) took %dns" % \
                      (opt.unroll_count+1, len(version.operations), len(loop.operations), nano))
        debug_stop("vec-opt-loop")
        #
        return info, loop.assemble_oplist()
    except NotAVectorizeableLoop:
        debug_stop("vec-opt-loop")
        # vectorization is not possible
        return loop_info, version.operations
    except NotAProfitableLoop:
        debug_stop("vec-opt-loop")
        # cost model says to skip this loop
        return loop_info, version.operations
    except Exception as e:
        debug_stop("vec-opt-loop")
        debug_print("failed to vectorize loop. THIS IS A FATAL ERROR!")
        if we_are_translated():
            from rpython.rtyper.lltypesystem import lltype
            from rpython.rtyper.lltypesystem.lloperation import llop
            llop.debug_print_traceback(lltype.Void)
        else:
            raise

def user_loop_bail_fast_path(loop, warmstate):
    """ In a fast path over the trace loop: try to prevent vecopt
        of spending time on a loop that will most probably fail.
    """

    resop_count = 0 # the count of operations minus debug_merge_points
    vector_instr = 0
    guard_count = 0
    blacklist = (rop.CALL, rop.CALL_ASSEMBLER)
    at_least_one_array_access = True
    for i,op in enumerate(loop.operations):
        if op.getopnum() == rop.DEBUG_MERGE_POINT:
            continue

        if op.vector >= 0 and not op.is_guard():
            vector_instr += 1

        resop_count += 1

        if op.is_primitive_array_access():
            at_least_one_array_access = True

        if warmstate.vec_ratio > 0.0:
            if op.getopnum() in blacklist:
                return True

        if op.is_guard():
            guard_count += 1

    if not at_least_one_array_access:
        return True

    if resop_count > warmstate.vec_length:
        return True

    if (float(vector_instr)/float(resop_count)) < warmstate.vec_ratio:
        return True

    if float(guard_count)/float(resop_count) > warmstate.vec_guard_ratio:
        return True

    return False

class VectorizingOptimizer(Optimizer):
    """ Try to unroll the loop and find instructions to group """

    def __init__(self, metainterp_sd, jitdriver_sd, cost_threshold):
        Optimizer.__init__(self, metainterp_sd, jitdriver_sd)
        self.cpu = metainterp_sd.cpu
        self.cost_threshold = cost_threshold
        self.packset = None
        self.unroll_count = 0
        self.smallest_type_bytes = 0
        self.sched_data = None
        self.appended_arg_count = 0
        self.orig_label_args = None
        self.has_two_labels = False

    def propagate_all_forward(self, info, loop):
        label = loop.label
        jump = loop.jump
        self.orig_label_args = label.getarglist_copy()
        if jump.getopnum() not in (rop.LABEL, rop.JUMP) or \
           label.getopnum() != rop.LABEL:
            raise NotAVectorizeableLoop()
        if jump.numargs() != label.numargs():
            raise NotAVectorizeableLoop()

        self.linear_find_smallest_type(loop)
        byte_count = self.smallest_type_bytes
        vsize = self.cpu.vector_register_size
        if vsize == 0 or byte_count == 0 or label.getopnum() != rop.LABEL:
            # stop, there is no chance to vectorize this trace
            # we cannot optimize normal traces (if there is no label)
            raise NotAVectorizeableLoop()

        # find index guards and move to the earliest position
        graph = self.analyse_index_calculations(loop)
        if graph is not None:
            state = SchedulerState(graph)
            self.schedule(state) # reorder the trace

        # unroll
        self.unroll_count = self.get_unroll_count(vsize)
        self.unroll_loop_iterations(loop, self.unroll_count)
        self.loop.operations = self.get_newoperations();
        self.clear_newoperations();

        # vectorize
        graph = DependencyGraph(loop)
        self.find_adjacent_memory_refs()
        self.extend_packset()
        self.combine_packset()
        # TODO move cost model to CPU
        costmodel = X86_CostModel(self.cpu, self.cost_threshold)
        state = VecScheduleState(graph, self.packset, self.cpu, costmodel)
        self.schedule(state)
        if not state.profitable():
            raise NotAProfitableLoop()

    def emit_unrolled_operation(self, op):
        self._last_emitted_op = op
        self._newoperations.append(op)

    def unroll_loop_iterations(self, loop, unroll_count):
        """ Unroll the loop X times. unroll_count + 1 = unroll_factor """
        op_count = len(loop.operations)

        label_op = loop.operations[0].clone()
        assert label_op.getopnum() == rop.LABEL
        jump_op = loop.operations[op_count-1]
        assert jump_op.getopnum() in (rop.LABEL, rop.JUMP)
        # use the target token of the label
        target_token = label_op.getdescr()
        if not we_are_translated():
            target_token.assumed_classes = {}
        if jump_op.getopnum() == rop.LABEL:
            jump_op = ResOperation(rop.JUMP, jump_op.getarglist(), target_token)
        else:
            jump_op = jump_op.clone()
            jump_op.setdescr(target_token)
        assert jump_op.is_final()

        self.emit_unrolled_operation(label_op)

        renamer = Renamer()
        operations = []
        for i in range(1,op_count-1):
            op = loop.operations[i].clone()
            if op.is_guard():
                assert isinstance(op, GuardResOp)
                failargs = renamer.rename_failargs(op, clone=True)
                snapshot = renamer.rename_rd_snapshot(op.rd_snapshot, clone=True)
                op.setfailargs(failargs)
                op.rd_snapshot = snapshot
            operations.append(op)
            self.emit_unrolled_operation(op)

        prohibit_opnums = (rop.GUARD_FUTURE_CONDITION,
                           rop.GUARD_EARLY_EXIT,
                           rop.GUARD_NOT_INVALIDATED)

        orig_jump_args = jump_op.getarglist()[:]
        # it is assumed that #label_args == #jump_args
        label_arg_count = len(orig_jump_args)
        for u in range(unroll_count):
            # fill the map with the renaming boxes. keys are boxes from the label
            for i in range(label_arg_count):
                la = label_op.getarg(i)
                ja = jump_op.getarg(i)
                ja = renamer.rename_box(ja)
                if la != ja:
                    renamer.start_renaming(la, ja)
            #
            for i, op in enumerate(operations):
                if op.getopnum() in prohibit_opnums:
                    continue # do not unroll this operation twice
                copied_op = op.clone()
                if copied_op.result is not None:
                    # every result assigns a new box, thus creates an entry
                    # to the rename map.
                    new_assigned_box = copied_op.result.clonebox()
                    renamer.start_renaming(copied_op.result, new_assigned_box)
                    copied_op.result = new_assigned_box
                #
                args = copied_op.getarglist()
                for a, arg in enumerate(args):
                    value = renamer.rename_box(arg)
                    copied_op.setarg(a, value)
                # not only the arguments, but also the fail args need
                # to be adjusted. rd_snapshot stores the live variables
                # that are needed to resume.
                if copied_op.is_guard():
                    assert isinstance(copied_op, GuardResOp)
                    descr = copied_op.getdescr()
                    if descr:
                        assert isinstance(descr, ResumeGuardDescr)
                        copied_op.setdescr(descr.clone())
                        # copy failargs/snapshot
                        copied_op.rd_snapshot = \
                          renamer.rename_rd_snapshot(copied_op.rd_snapshot,
                                                     clone=True)
                        renamed_failargs = \
                            renamer.rename_failargs(copied_op, clone=True)
                        copied_op.setfailargs(renamed_failargs)
                #
                self.emit_unrolled_operation(copied_op)

        # the jump arguments have been changed
        # if label(iX) ... jump(i(X+1)) is called, at the next unrolled loop
        # must look like this: label(i(X+1)) ... jump(i(X+2))
        args = jump_op.getarglist()
        for i, arg in enumerate(args):
            value = renamer.rename_box(arg)
            jump_op.setarg(i, value)
        #
        self.emit_unrolled_operation(jump_op)

    def linear_find_smallest_type(self, loop):
        # O(#operations)
        for i,op in enumerate(loop.operations):
            if op.is_primitive_array_access():
                descr = op.getdescr()
                byte_count = descr.get_item_size_in_bytes()
                if self.smallest_type_bytes == 0 \
                   or byte_count < self.smallest_type_bytes:
                    self.smallest_type_bytes = byte_count

    def get_unroll_count(self, simd_vec_reg_bytes):
        """ This is an estimated number of further unrolls """
        # this optimization is not opaque, and needs info about the CPU
        byte_count = self.smallest_type_bytes
        if byte_count == 0:
            return 0
        unroll_count = simd_vec_reg_bytes // byte_count
        return unroll_count-1 # it is already unrolled once

    def find_adjacent_memory_refs(self, graph):
        """ The pre pass already builds a hash of memory references and the
            operations. Since it is in SSA form there are no array indices.
            If there are two array accesses in the unrolled loop
            i0,i1 and i1 = int_add(i0,c), then i0 = i0 + 0, i1 = i0 + 1.
            They are represented as a linear combination: i*c/d + e, i is a variable,
            all others are integers that are calculated in reverse direction
        """
        loop = self.loop
        operations = loop.operations

        self.packset = PackSet(self.cpu.vector_register_size)
        memory_refs = graph.memory_refs.items()
        # initialize the pack set
        for node_a,memref_a in memory_refs:
            for node_b,memref_b in memory_refs:
                if memref_a is memref_b:
                    continue
                #print "???", memref_a.index_var, memref_b.index_var
                # instead of compare every possible combination and
                # exclue a_opidx == b_opidx only consider the ones
                # that point forward:
                if memref_a.is_adjacent_after(memref_b):
                    pair = self.packset.can_be_packed(node_a, node_b, None, False)
                    if pair:
                        self.packset.add_pack(pair)

    def extend_packset(self):
        """ Follow dependency chains to find more candidates to put into
            pairs.
        """
        pack_count = self.packset.pack_count()
        while True:
            for pack in self.packset.packs:
                self.follow_def_uses(pack)
            if pack_count == self.packset.pack_count():
                pack_count = self.packset.pack_count()
                for pack in self.packset.packs:
                    self.follow_use_defs(pack)
                if pack_count == self.packset.pack_count():
                    break
            pack_count = self.packset.pack_count()

    def follow_use_defs(self, pack):
        assert isinstance(pack, Pair)
        for ldep in pack.left.depends():
            for rdep in pack.right.depends():
                lnode = ldep.to
                rnode = rdep.to
                # only valid if the result of the left is in args of pack left
                result = lnode.getoperation().result
                args = pack.left.getoperation().getarglist()
                if result is None or result not in args:
                    continue
                isomorph = isomorphic(lnode.getoperation(), rnode.getoperation())
                if isomorph and lnode.is_before(rnode):
                    pair = self.packset.can_be_packed(lnode, rnode, pack, False)
                    if pair:
                        self.packset.add_pack(pair)

    def follow_def_uses(self, pack):
        assert isinstance(pack, Pair)
        for ldep in pack.left.provides():
            for rdep in pack.right.provides():
                lnode = ldep.to
                rnode = rdep.to
                result = pack.left.getoperation().result
                args = lnode.getoperation().getarglist()
                if result is None or result not in args:
                    continue
                isomorph = isomorphic(lnode.getoperation(), rnode.getoperation())
                if isomorph and lnode.is_before(rnode):
                    pair = self.packset.can_be_packed(lnode, rnode, pack, True)
                    if pair:
                        self.packset.add_pack(pair)

    def combine_packset(self):
        """ Combination is done iterating the packs that have
            a sorted op index of the first operation (= left).
            If a pack is marked as 'full', the next pack that is
            encountered having the full_pack.right == pack.left,
            the pack is removed. This is because the packs have
            intersecting edges.
        """
        if len(self.packset.packs) == 0:
            raise NotAVectorizeableLoop()
        i = 0
        j = 0
        end_ij = len(self.packset.packs)
        orphan = {}
        while True:
            len_before = len(self.packset.packs)
            i = 0
            while i < end_ij:
                while j < end_ij and i < end_ij:
                    if i == j:
                        # do not pack with itself! won't work...
                        j += 1
                        continue
                    pack1 = self.packset.packs[i]
                    pack2 = self.packset.packs[j]
                    if pack1.rightmost_match_leftmost(pack2):
                        end_ij = self.packset.combine(i,j)
                    else:
                        # do not inc in rightmost_match_leftmost
                        # this could miss some pack
                        j += 1
                i += 1
                j = 0
            if len_before == len(self.packset.packs):
                break

        self.packset.split_overloaded_packs()

        if not we_are_translated():
            # some test cases check the accumulation variables
            self.packset.accum_vars = {}
            print "packs:"
            check = {}
            fail = False
            for pack in self.packset.packs:
                left = pack.operations[0]
                right = pack.operations[-1]
                if left in check or right in check:
                    fail = True
                check[left] = None
                check[right] = None
                accum = pack.accum
                if accum:
                    self.packset.accum_vars[accum.var] = accum.pos

                print " %dx %s " % (len(pack.operations),
                                    pack.operations[0].op.getopname())
                if accum:
                    print "   accumulates!"
            if fail:
                assert False

    def schedule(self, state):
        state.prepare()
        scheduler = Scheduler()
        scheduler.walk_and_emit(state)
        if not state.profitable():
            return
        state.post_schedule()

    def prepend_invariant_operations(self, sched_data):
        """ Add invariant operations to the trace loop. returns the operation list
            as first argument and a second a boolean value.
        """
        oplist = self._newoperations

        if len(sched_data.invariant_oplist) > 0:
            label = oplist[0]
            assert label.getopnum() == rop.LABEL
            #
            jump = oplist[-1]
            assert jump.getopnum() == rop.JUMP
            #
            label_args = label.getarglist()[:]
            jump_args = jump.getarglist()
            for var in sched_data.invariant_vector_vars:
                label_args.append(var)
                jump_args.append(var)
            #
            # in case of any invariant_vector_vars, the label is restored
            # and the invariant operations are added between the original label
            # and the new label
            descr = label.getdescr()
            assert isinstance(descr, TargetToken)
            token = TargetToken(descr.targeting_jitcell_token)
            oplist[0] = label.copy_and_change(label.getopnum(), args=label_args, descr=token)
            oplist[-1] = jump.copy_and_change(jump.getopnum(), args=jump_args, descr=token)
            #
            return [ResOperation(rop.LABEL, self.orig_label_args, None, descr)] + \
                   sched_data.invariant_oplist + oplist
        #
        return oplist

    def analyse_index_calculations(self, loop):
        """ Tries to move guarding instructions an all the instructions that
            need to be computed for the guard to the loop header. This ensures
            that guards fail 'early' and relax dependencies. Without this
            step vectorization would not be possible!
        """
        graph = DependencyGraph(loop)
        ee_guard_node = graph.getnode(0)
        if ee_guard_node.getopnum() != rop.GUARD_EARLY_EXIT:
            raise NotAVectorizeableLoop()
        label_node = graph.getnode(0)
        guards = graph.guards
        for guard_node in guards:
            if guard_node is ee_guard_node:
                continue
            modify_later = []
            last_prev_node = None
            valid = True
            for prev_dep in guard_node.depends():
                prev_node = prev_dep.to
                if prev_dep.is_failarg():
                    # remove this edge later.
                    # 1) only because of failing, this dependency exists
                    # 2) non pure operation points to this guard.
                    #    but if this guard only depends on pure operations, it can be checked
                    #    at an earlier position, the non pure op can execute later!
                    modify_later.append((prev_node, guard_node))
                else:
                    for path in prev_node.iterate_paths(ee_guard_node, backwards=True, blacklist=True):
                        if path.is_always_pure(exclude_first=True, exclude_last=True):
                            path.set_schedule_priority(10)
                            if path.last() is ee_guard_node:
                                modify_later.append((path.last_but_one(), None))
                        else:
                            # transformation is invalid.
                            # exit and do not enter else branch!
                            valid = False
                    if not valid:
                        break
            if valid:
                # transformation is valid, modify the graph and execute
                # this guard earlier
                for a,b in modify_later:
                    if b is not None:
                        a.remove_edge_to(b)
                    else:
                        last_but_one = a
                        if last_but_one is ee_guard_node:
                            continue
                        ee_guard_node.remove_edge_to(last_but_one)
                        #label_node.edge_to(last_but_one, label='pullup')
                # only the last guard needs a connection
                guard_node.edge_to(ee_guard_node, label='pullup-last-guard')
                self.relax_guard_to(guard_node, ee_guard_node)

    def relax_guard_to(self, guard_node, other_node):
        """ Relaxes a guard operation to an earlier guard. """
        # clone this operation object. if the vectorizer is
        # not able to relax guards, it won't leave behind a modified operation
        tgt_op = guard_node.getoperation().clone()
        guard_node.op = tgt_op

        op = other_node.getoperation()
        assert isinstance(tgt_op, GuardResOp)
        assert isinstance(op, GuardResOp)
        olddescr = op.getdescr()
        descr = None
        guard_true_false = tgt_op.getopnum() in (rop.GUARD_TRUE, rop.GUARD_FALSE)
        if guard_true_false:
            descr = CompileLoopVersionDescr()
        else:
            descr = ResumeAtLoopHeaderDescr()
        if olddescr:
            descr.copy_all_attributes_from(olddescr)
        #
        tgt_op.setdescr(descr)
        tgt_op.setfailargs(op.getfailargs()[:])


class CostModel(object):
    """ Utility to estimate the savings for the new trace loop.
        The main reaons to have this is of frequent unpack instructions,
        and the missing ability (by design) to detect not vectorizable loops.
    """
    def __init__(self, cpu, threshold):
        self.threshold = threshold
        self.vec_reg_size = cpu.vector_register_size
        self.savings = 0

    def reset_savings(self):
        self.savings = 0

    def record_cast_int(self, op):
        raise NotImplementedError

    def record_pack_savings(self, pack, times):
        raise NotImplementedError

    def record_vector_pack(self, box, index, count):
        raise NotImplementedError

    def record_vector_unpack(self, box, index, count):
        raise NotImplementedError

    def unpack_cost(self, op, index, count):
        raise NotImplementedError

    def savings_for_pack(self, pack, times):
        raise NotImplementedError

    def profitable(self):
        return self.savings >= 0

class X86_CostModel(CostModel):
    def record_pack_savings(self, pack, times):
        cost, benefit_factor = (1,1)
        node = pack.operations[0]
        op = node.getoperation()
        if op.getopnum() == rop.INT_SIGNEXT:
            cost, benefit_factor = self.cb_signext(pack)
        #
        self.savings += benefit_factor * times - cost

    def cb_signext(self, pack):
        left = pack.leftmost()
        if left.cast_to_bytesize() == left.cast_from_bytesize():
            return 0, 0
        # no benefit for this operation! needs many x86 instrs
        return 1,0

    def record_cast_int(self, fromsize, tosize, count):
        # for each move there is 1 instruction
        self.savings += -count

    def record_vector_pack(self, src, index, count):
        if src.datatype == FLOAT:
            if index == 1 and count == 1:
                self.savings -= 2
                return
        self.savings -= count

    def record_vector_unpack(self, src, index, count):
        self.record_vector_pack(src, index, count)

def isomorphic(l_op, r_op):
    """ Subject of definition, here it is equal operation.
        See limintations (vectorization.rst).
    """
    if l_op.getopnum() == r_op.getopnum():
        return True
    return False

class PackSet(object):
    _attrs_ = ('packs', 'vec_reg_size')
    def __init__(self, vec_reg_size):
        self.packs = []
        self.vec_reg_size = vec_reg_size

    def pack_count(self):
        return len(self.packs)

    def add_pack(self, pack):
        self.packs.append(pack)

    def can_be_packed(self, lnode, rnode, origin_pack, forward):
        """ Check to ensure that two nodes might be packed into a Pair.
        """
        if isomorphic(lnode.getoperation(), rnode.getoperation()):
            if lnode.independent(rnode):
                if forward and isinstance(origin_pack, AccumPair):
                    # in this case the splitted accumulator must
                    # be combined. This case is not supported
                    raise NotAVectorizeableLoop()
                #
                if self.contains_pair(lnode, rnode):
                    return None
                #
                if origin_pack is None:
                    op = lnode.getoperation()
                    if op.is_primitive_load():
                        return Pair(lnode, rnode)
                    else:
                        return Pair(lnode, rnode)
                if self.profitable_pack(lnode, rnode, origin_pack, forward):
                    return Pair(lnode, rnode)
            else:
                if self.contains_pair(lnode, rnode):
                    return None
                if origin_pack is not None:
                    return self.accumulates_pair(lnode, rnode, origin_pack)
        return None

    def contains_pair(self, lnode, rnode):
        for pack in self.packs:
            if pack.left is lnode or pack.right is rnode:
                return True
        return False

    def profitable_pack(self, lnode, rnode, origin_pack, forward):
        lpacknode = origin_pack.left
        if self.prohibit_packing(origin_pack,
                                 lpacknode.getoperation(),
                                 lnode.getoperation(),
                                 forward):
            return False
        rpacknode = origin_pack.right
        if self.prohibit_packing(origin_pack,
                                 rpacknode.getoperation(),
                                 rnode.getoperation(),
                                 forward):
            return False

        return True

    def prohibit_packing(self, pack, packed, inquestion, forward):
        """ Blocks the packing of some operations """
        if inquestion.vector == -1:
            return True
        if packed.is_primitive_array_access():
            if packed.getarg(1) == inquestion.result:
                return True
        if not forward and inquestion.getopnum() == rop.INT_SIGNEXT:
            # prohibit the packing of signext in backwards direction
            # the type cannot be determined!
            return True
        return False

    def combine(self, i, j):
        """ Combine two packs. it is assumed that the attribute self.packs
            is not iterated when calling this method.
        """
        pack_i = self.packs[i]
        pack_j = self.packs[j]
        operations = pack_i.operations
        for op in pack_j.operations[1:]:
            operations.append(op)
        pack = Pack(operations)
        self.packs[i] = pack
        # preserve the accum variable (if present)
        pack.accum = pack_i.accum
        pack_i.accum = pack_j.accum = None

        del self.packs[j]
        return len(self.packs)

    def accumulates_pair(self, lnode, rnode, origin_pack):
        # lnode and rnode are isomorphic and dependent
        assert isinstance(origin_pack, Pair)
        lop = lnode.getoperation()
        opnum = lop.getopnum()

        if opnum in (rop.FLOAT_ADD, rop.INT_ADD, rop.FLOAT_MUL):
            roper = rnode.getoperation()
            assert lop.numargs() == 2 and lop.result is not None
            accum_var, accum_pos = self.getaccumulator_variable(lop, roper, origin_pack)
            if not accum_var:
                return None
            # the dependency exists only because of the result of lnode
            for dep in lnode.provides():
                if dep.to is rnode:
                    if not dep.because_of(accum_var):
                        # not quite ... this is not handlable
                        return None
            # get the original variable
            accum_var = lop.getarg(accum_pos)

            # in either of the two cases the arguments are mixed,
            # which is not handled currently
            var_pos = (accum_pos + 1) % 2
            plop = origin_pack.left.getoperation()
            if lop.getarg(var_pos) is not plop.result:
                return None
            prop = origin_pack.right.getoperation()
            if roper.getarg(var_pos) is not prop.result:
                return None

            # this can be handled by accumulation
            ptype = origin_pack.output_type
            if ptype.getsize() != 8:
                # do not support if if the type size is smaller
                # than the cpu word size.
                # WHY?
                # to ensure accum is done on the right size, the dependencies
                # of leading/preceding signext/floatcast instructions needs to be
                # considered. => tree pattern matching problem.
                return None
            accum = Accum(opnum, accum_var, accum_pos)
            return AccumPair(lnode, rnode, ptype, ptype, accum)

        return None

    def getaccumulator_variable(self, lop, rop, origin_pack):
        args = rop.getarglist()
        for i, arg in enumerate(args):
            if arg is lop.result:
                return arg, i
        #
        return None, -1

    def accumulate_prepare(self, state):
        vec_reg_size = state.vec_reg_size
        for pack in self.packs:
            if not pack.is_accumulating():
                continue
            xxx
            accum = pack.accum
            # create a new vector box for the parameters
            box = pack.input_type.new_vector_box()
            size = vec_reg_size // pack.input_type.getsize()
            # reset the box to zeros or ones
            if accum.operator == Accum.PLUS:
                op = ResOperation(rop.VEC_BOX, [ConstInt(size)], box)
                state.invariant_oplist.append(op)
                result = box.clonebox()
                op = ResOperation(rop.VEC_INT_XOR, [box, box], result)
                state.invariant_oplist.append(op)
                box = result
            elif accum.operator == Accum.MULTIPLY:
                # multiply is only supported by floats
                op = ResOperation(rop.VEC_FLOAT_EXPAND, [ConstFloat(1.0), ConstInt(size)], box)
                state.invariant_oplist.append(op)
            else:
                raise NotImplementedError("can only handle %s" % accum.operator)
            result = box.clonebox()
            assert isinstance(result, BoxVector)
            result.accum = accum
            # pack the scalar value
            op = ResOperation(getpackopnum(box.gettype()),
                              [box, accum.var, ConstInt(0), ConstInt(1)], result)
            state.invariant_oplist.append(op)
            # rename the variable with the box
            state.setvector_of_box(accum.getoriginalbox(), 0, result) # prevent it from expansion
            state.renamer.start_renaming(accum.getoriginalbox(), result)

    def split_overloaded_packs(self):
        newpacks = []
        for i,pack in enumerate(self.packs):
            load = pack.pack_load(self.vec_reg_size)
            if load > Pack.FULL:
                print "overloaded pack", pack
                pack.split(newpacks, self.vec_reg_size)
                continue
            if load < Pack.FULL:
                print "underloaded pack", pack
                for op in pack.operations:
                    op.priority = -100
                pack.clear()
                self.packs[i] = None
                continue
            print "fully packed", pack
        self.packs = [pack for pack in self.packs + newpacks if pack]

