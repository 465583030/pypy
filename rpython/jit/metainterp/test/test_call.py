
from rpython.jit.metainterp.test.support import LLJitMixin
from rpython.rlib import jit

class TestCall(LLJitMixin):
    def test_indirect_call(self):
        @jit.dont_look_inside
        def f1(x):
            return x + 1

        @jit.dont_look_inside
        def f2(x):
            return x + 2

        @jit.dont_look_inside
        def choice(i):
            if i:
                return f1
            return f2

        def f(i):
            func = choice(i)
            return func(i)

        res = self.interp_operations(f, [3])
        assert res == f(3)

    def test_cond_call(self):
        def f(l, n):
            l.append(n)

        def main(n):
            l = []
            jit.conditional_call(n == 10, f, l, n)
            return len(l)

        assert self.interp_operations(main, [10]) == 1
        assert self.interp_operations(main, [5]) == 0

    def test_cond_call_disappears(self):
        driver = jit.JitDriver(greens = [], reds = ['n'])

        def f(n):
            raise ValueError

        def main(n):
            while n > 0:
                driver.jit_merge_point(n=n)
                jit.conditional_call(False, f, 10)
                n -= 1
            return 42

        assert self.meta_interp(main, [10]) == 42
        self.check_resops(guard_no_exception=0)

    def test_cond_call_i(self):
        def f(l, n):
            l.append(n)
            return 1000

        def main(n):
            l = []
            x = jit.conditional_call_value(n, 10, f, l, n)
            return x + len(l)

        assert self.interp_operations(main, [10]) == 1001
        assert self.interp_operations(main, [5]) == 5

    def test_cond_call_r(self):
        def f(n):
            return [n]

        def main(n):
            if n == 10:
                l = []
            else:
                l = None
            l = jit.conditional_call_value(l, None, f, n)
            return len(l)

        assert self.interp_operations(main, [10]) == 0
        assert self.interp_operations(main, [5]) == 1
