
from pypy.rpython.test.tool import BaseRtypingTest, LLRtypeMixin, OORtypeMixin
from pypy.rlib.rstruct import runpack
import struct

class BaseTestRStruct(BaseRtypingTest):
    def test_unpack(self):
        def fn():
            return runpack('ll', '\x03\x00\x00\x00\x05\x00\x00\x00')[0]
        assert fn() == 3
        assert self.interpret(fn, []) == 3

class TestLLType(BaseTestRStruct, LLRtypeMixin):
    pass

class TestOOType(BaseTestRStruct, OORtypeMixin):
    pass
