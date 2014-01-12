
from rpython.jit.metainterp.history import JitCellToken
from rpython.jit.metainterp.history import BasicFailDescr
from rpython.jit.codewriter.jitcode import JitCode
from rpython.jit.tool.oparser import parse
from rpython.jit.metainterp.optimizeopt.util import equaloplists
from rpython.rtyper.lltypesystem import lltype

class MockJitCode(JitCode):
    def __init__(self, no):
        self.no = no
        self.name = 'frame %d' % no

    def num_regs(self):
        return self.no

    def __repr__(self):
        return 'MockJitCode(%d)' % self.no

class ResumeTest(object):
    def setup_method(self, meth):
        self.cpu = self.CPUClass(None, None)
        self.cpu.setup_once()

    def test_simple(self):
        jitcode = MockJitCode(3)
        loop = parse("""
        [i0]
        enter_frame(-1, descr=jitcode)
        resume_put(i0, 0, 2)
        guard_true(i0)
        leave_frame()
        """, namespace={'jitcode': jitcode})
        looptoken = JitCellToken()
        self.cpu.compile_loop(None, loop.inputargs, loop.operations,
                              looptoken)
        descr = loop.operations[2].getdescr()
        assert descr.rd_bytecode_position == 2
        expected_resume = parse("""
        []
        enter_frame(-1, descr=jitcode)
        resume_put(28, 0, 2)
        leave_frame()
        """, namespace={'jitcode': jitcode})
        equaloplists(descr.rd_resume_bytecode.opcodes,
                     expected_resume.operations)

    def test_resume_new(self):
        jitcode = JitCode("name")
        jitcode.setup(num_regs_i=1, num_regs_r=0, num_regs_f=0)
        S = lltype.GcStruct('S', ('field', lltype.Signed))
        structdescr = self.cpu.sizeof(S)
        fielddescr = self.cpu.fielddescrof(S, 'field')
        namespace = {'jitcode':jitcode, 'structdescr':structdescr,
                     'fielddescr':fielddescr}
        loop = parse("""
        [i0, i1]
        enter_frame(-1, descr=jitcode)
        p0 = resume_new(descr=structdescr)
        resume_setfield_gc(p0, i0, descr=fielddescr)
        resume_put(p0, 0, 0)
        i2 = int_lt(i1, 13)
        guard_true(i2)
        leave_frame()
        finish()
        """, namespace=namespace)
        looptoken = JitCellToken()
        self.cpu.compile_loop(None, loop.inputargs, loop.operations,
                              looptoken)
        expected_resume = parse("""
        [i0]
        enter_frame(-1, descr=jitcode)
        p0 = resume_new(descr=structdescr)
        resume_setfield_gc(p0, i0, descr=fielddescr)
        resume_put(p0, 0, 0)
        leave_frame()
        """, namespace=namespace)
        descr = loop.operations[-3].getdescr()
        assert descr.rd_bytecode_position == 4
        equaloplists(descr.rd_resume_bytecode.opcodes,
                     expected_resume.operations)

    def test_spill(self):
        jitcode = JitCode("name")
        jitcode.setup(num_regs_i=2, num_regs_r=0, num_regs_f=0)
        faildescr1 = BasicFailDescr(1)
        faildescr2 = BasicFailDescr(2)
        loop = parse("""
        [i0]
        enter_frame(-1, descr=jitcode)
        i2 = int_add(i0, 1)
        resume_put(i2, 0, 1)
        guard_true(i0, descr=faildescr1)
        force_spill(i2)
        guard_true(i0, descr=faildescr2)
        leave_frame()
        """, namespace={'jitcode':jitcode, 'faildescr1':faildescr1,
                        'faildescr2':faildescr2})
        looptoken = JitCellToken()
        self.cpu.compile_loop(None, loop.inputargs, loop.operations,
                              looptoken)

        expected_resume = parse("""
        [i2]
        enter_frame(-1, descr=jitcode)
        resume_put(1, 0, 1)
        resume_put(29, 0, 1)
        leave_frame()
        """, namespace={'jitcode':jitcode})
        descr1 = loop.operations[3].getdescr()
        descr2 = loop.operations[5].getdescr()
        assert descr1.rd_bytecode_position == 2
        assert descr2.rd_bytecode_position == 3
        equaloplists(descr1.rd_resume_bytecode.opcodes,
                     expected_resume.operations)

    def test_bridge(self):
        jitcode = JitCode("name")
        jitcode.setup(num_regs_i=2, num_regs_r=0, num_regs_f=0)
        loop = parse("""
        [i0]
        enter_frame(-1, descr=jitcode)
        resume_put(i0, 0, 0)
        i1 = int_lt(i0, 10)
        guard_true(i1)
        leave_frame()
        """, namespace={'jitcode': jitcode})

        looptoken = JitCellToken()
        self.cpu.compile_loop(None, loop.inputargs, loop.operations,
                              looptoken)
