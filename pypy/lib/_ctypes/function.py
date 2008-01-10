import _ffi

class CFuncPtrType(type):
    # XXX write down here defaults and such things
    pass

class CFuncPtr(object):
    __metaclass__ = CFuncPtrType
    argtypes = None
    _argtypes = None
    restype = None
    _restype = None
    def __init__(self, stuff):
        if isinstance(stuff, tuple):
            print stuff
            name, dll = stuff
            self.name = name
            self.dll = dll
            self._funcptr = None
        else:
            self.name = None
            self.dll = None

    def __call__(self, *args):
        import ctypes
        if self.restype is None:
            # XXX point to default instead
            self.restype = ctypes.c_int
        if self.argtypes is not None:
            if len(args) != len(self.argtypes):
                raise TypeError("%s takes %s arguments, given %s" %
                                (self.name, len(self.argtypes), len(args)))
        res = self._getfuncptr(args)(*[arg.value for arg in args])
        if issubclass(self.restype, ctypes._SimpleCData):
            return res
        else:
            # XXX pointers
            return self.restype(address=res)

    def _getfuncptr(self, args):
        if self._funcptr is not None:
            if (self.argtypes is self._argtypes
                and self.restype is self._restype):
                return self._funcptr
        if self.argtypes is None:
            argtypes = self._guess_magic_args(args)
        else:
            argtypes = self.argtypes
        argtps = [argtype._ffiletter for argtype in argtypes]
        restp = self.restype._ffiletter
        self._funcptr = funcptr = self.dll._handle.ptr(self.name, argtps, restp)
        self._argtypes = self.argtypes
        self._restype = self.restype
        return funcptr

    def _guess_magic_args(self, args):
        import _ctypes
        res = []
        for arg in args:
            if isinstance(arg, _ctypes._CData):
                res.append(type(arg))
            else:
                raise TypeError("Cannot convert %s" % arg)
        return res
