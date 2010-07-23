from pypy.interpreter.error import OperationError
from pypy.interpreter.typedef import TypeDef, GetSetProperty
from pypy.rpython.lltypesystem import lltype, rffi
from pypy.interpreter.gateway import interp2app, ObjSpace, W_Root, ApplevelClass
from pypy.rlib.jit import dont_look_inside
from pypy.rlib import rgc
from pypy.rlib.unroll import unrolling_iterable
from pypy.rlib.rstruct.runpack import runpack
from pypy.interpreter.argument import Arguments, Signature
from pypy.interpreter.baseobjspace import ObjSpace, W_Root, Wrappable
from pypy.objspace.std.stdtypedef import SMM, StdTypeDef
from pypy.objspace.std.register_all import register_all
from pypy.objspace.std.model import W_Object

def w_array(space, typecode, w_initializer=None):
    if len(typecode) != 1:
        msg = 'array() argument 1 must be char, not str'
        raise OperationError(space.w_TypeError, space.wrap(msg))
    typecode=typecode[0]
    
    for tc in unroll_typecodes:
        if typecode == tc:
            a = types[tc].w_class(space)
            if not space.is_w(w_initializer, space.w_None):
                if space.type(w_initializer) is space.w_str:
                    space.call_method(a, 'fromstring', w_initializer)
                elif space.type(w_initializer) is space.w_unicode:
                    space.call_method(a, 'fromunicode', w_initializer)
                elif space.type(w_initializer) is space.w_list:
                    space.call_method(a, 'fromlist', w_initializer)
                else:
                    space.call_method(a, 'extend', w_initializer)
            break
    else:
        msg = 'bad typecode (must be c, b, B, u, h, H, i, I, l, L, f or d)'
        raise OperationError(space.w_ValueError, space.wrap(msg))

    return a
w_array.unwrap_spec = (ObjSpace, str, W_Root)

def w_array_sub(space, w_cls, typecode, w_initializer=None):
    return w_array(space, typecode, w_initializer)
w_array_sub.unwrap_spec = (ObjSpace, W_Root, str, W_Root)


array_append = SMM('append', 2)
array_extend = SMM('extend', 2)

array_tolist = SMM('tolist', 1)
array_fromlist = SMM('fromlist', 2)
array_tostring = SMM('tostring', 1)
array_fromstring = SMM('fromstring', 2)
array_tounicode = SMM('tounicode', 1)
array_fromunicode = SMM('fromunicode', 2)
array_tofile = SMM('tofile', 1)
array_fromfile = SMM('fromfile', 3)


def descr_itemsize(space, self):
    return space.wrap(self.itemsize)
def descr_typecode(space, self):
    return space.wrap(self.typecode)


type_typedef = StdTypeDef(
    'array',
    __new__ = interp2app(w_array_sub),
    itemsize = GetSetProperty(descr_itemsize),
    typecode = GetSetProperty(descr_typecode),
    )
type_typedef.registermethods(globals())


class W_ArrayBase(W_Object):
    typedef = type_typedef
    @staticmethod
    def register(typeorder):
        typeorder[W_ArrayBase] = []

class TypeCode(object):
    def __init__(self, itemtype, unwrap, canoverflow=False, signed=False):
        self.itemtype = itemtype
        self.bytes = rffi.sizeof(itemtype)
        #self.arraytype = lltype.GcArray(itemtype)
        self.arraytype = lltype.Array(itemtype, hints={'nolength': True})
        self.unwrap = unwrap
        self.signed = signed
        self.canoverflow = canoverflow
        self.w_class = None

        if self.canoverflow:
            assert self.bytes <= rffi.sizeof(rffi.ULONG)
            if self.bytes == rffi.sizeof(rffi.ULONG) and not signed and self.unwrap == 'int_w':
                # Treat this type as a ULONG
                self.unwrap = 'bigint_w'
                self.canoverflow = False


    def _freeze_(self):
        # hint for the annotator: track individual constant instances 
        return True


types = {
    'c': TypeCode(lltype.Char,        'str_w'),
    'u': TypeCode(lltype.UniChar,     'unicode_w'),
    'b': TypeCode(rffi.SIGNEDCHAR,    'int_w', True, True),
    'B': TypeCode(rffi.UCHAR,         'int_w', True),
    'h': TypeCode(rffi.SHORT,         'int_w', True, True),
    'H': TypeCode(rffi.USHORT,        'int_w', True),
    'i': TypeCode(rffi.INT,           'int_w', True, True),
    'I': TypeCode(rffi.UINT,          'int_w', True), 
    'l': TypeCode(rffi.LONG,          'int_w', True, True),
    'L': TypeCode(rffi.ULONG,         'bigint_w'), # Overflow handled by rbigint.touint() which
                                                   # corresponds to the C-type unsigned long
    'f': TypeCode(lltype.SingleFloat, 'float_w'),
    'd': TypeCode(lltype.Float,       'float_w'),
    }
for k, v in types.items(): v.typecode=k
unroll_typecodes = unrolling_iterable(types.keys())

def make_array(mytype):
    class W_ArrayIter(Wrappable):
        def __init__(self, a):
            self.space = a.space
            self.a = a
            self.pos = 0

        def iter_w(self):
            return self.space.wrap(self)

        def next_w(self):
            if self.pos >= self.a.len:
                raise OperationError(self.space.w_StopIteration, self.space.w_None)
            val = self.a.descr_getitem(self.space.wrap(self.pos))
            self.pos += 1
            return val
        
    class W_Array(W_ArrayBase):
        itemsize = mytype.bytes
        typecode = mytype.typecode

        @staticmethod
        def register(typeorder):
            typeorder[W_Array] = []

        def __init__(self, space):
            self.space = space
            self.len = 0
            self.allocated = 0
            self.buffer = lltype.nullptr(mytype.arraytype)

        def item_w(self, w_item):
            space = self.space
            unwrap = getattr(space, mytype.unwrap)
            item = unwrap(w_item)
            if mytype.unwrap == 'bigint_w':
                try:
                    item = item.touint()
                except (ValueError, OverflowError):
                    msg = 'unsigned %d-byte integer out of range' % mytype.bytes
                    raise OperationError(space.w_OverflowError, space.wrap(msg))
            elif mytype.unwrap == 'str_w' or mytype.unwrap == 'unicode_w':
                if len(item) != 1:
                    msg = 'array item must be char'
                    raise OperationError(space.w_TypeError, space.wrap(msg))
                item=item[0]

            if mytype.canoverflow:
                msg = None
                if mytype.signed:
                    if item < -1 << (mytype.bytes * 8 - 1):
                        msg = ('signed %d-byte integer is less than minimum' %
                               mytype.bytes)
                    elif item > (1 << (mytype.bytes * 8 - 1)) - 1:
                        msg = ('signed %d-byte integer is greater than maximum'
                               % mytype.bytes)
                else:
                    if item < 0:
                        msg = ('unsigned %d-byte integer is less than minimum'
                               % mytype.bytes)
                    elif item > (1 << (mytype.bytes * 8)) - 1:
                        msg = ('unsigned %d-byte integer is greater'
                               ' than maximum' % mytype.bytes)
                if msg is not None:
                    raise OperationError(space.w_OverflowError, space.wrap(msg))
            return rffi.cast(mytype.itemtype, item)


        def __del__(self):
            self.setlen(0)


        def setlen(self, size):
            if size > 0:
                if size > self.allocated or size < self.allocated/2:
                    if size < 9:
                        some = 3
                    else:
                        some = 6
                    some += size >> 3
                    self.allocated = size + some
                    new_buffer = lltype.malloc(mytype.arraytype, self.allocated, flavor='raw')
                    for i in range(min(size,self.len)):
                        new_buffer[i] = self.buffer[i]
                else:
                    self.len = size
                    return
            else:
                assert size == 0
                self.allocated = 0
                new_buffer = lltype.nullptr(mytype.arraytype)

            if self.buffer:
                lltype.free(self.buffer, flavor='raw')                
            self.buffer = new_buffer
            self.len = size


        def fromsequence(self, w_seq):
            space = self.space
            oldlen = self.len
            try:
                new = space.int_w(space.len(w_seq))
                self.setlen(self.len + new)
            except OperationError:
                pass

            i = 0
            try:
                if mytype.typecode == 'u':
                    myiter = space.unpackiterable
                else:
                    myiter = space.listview
                for w_i in myiter(w_seq):
                    if oldlen + i >= self.len:
                        self.setlen(oldlen + i + 1)
                    self.buffer[oldlen + i] = self.item_w(w_i)
                    i += 1
            except OperationError:
                self.setlen(oldlen + i)
                raise
            self.setlen(oldlen + i)

        def charbuf(self):
            return  rffi.cast(rffi.CCHARP, self.buffer)


    # List interface

    def len__Array(space, self):
        return space.wrap(self.len)

    def getitem__Array_ANY(space, self, w_idx):
        idx, stop, step = space.decode_index(w_idx, self.len)
        assert step == 0
        item = self.buffer[idx]
        tc=mytype.typecode
        if (tc == 'b' or tc == 'B' or tc == 'h' or tc == 'H' or
            tc == 'i' or tc == 'l'):
            item = rffi.cast(lltype.Signed, item)
        elif mytype.typecode == 'f':
            item = float(item)
        return self.space.wrap(item)

    def getitem__Array_Slice(space, self, w_slice):
        start, stop, step = space.decode_index(w_slice, self.len)
        if step < 0:
            w_lst = array_tolist__Array(space, self)
            w_lst = space.getitem(w_lst, w_slice)
            w_a=mytype.w_class(self.space)
            w_a.fromsequence(w_lst)
        else:
            size = (stop - start) / step
            if (stop - start) % step > 0: size += 1
            w_a=mytype.w_class(self.space)
            w_a.setlen(size)
            for j,i in enumerate(range(start, stop, step)):
                w_a.buffer[j]=self.buffer[i]
        return w_a

    def getslice__Array_ANY_ANY(space, self, w_i, w_j):
        return space.getitem(self, space.newslice(w_i, w_j, space.w_None))

    def setitem__Array_ANY_ANY(space, self, w_idx, w_item):
        idx, stop, step = space.decode_index(w_idx, self.len)
        if step != 0:
            msg='can only assign array to array slice'
            raise OperationError(self.space.w_TypeError, self.space.wrap(msg))
        item = self.item_w(w_item)
        self.buffer[idx] = item

    def setitem__Array_Slice_Array(space, self, w_idx, w_item):
        start, stop, step = self.space.decode_index(w_idx, self.len)
        size = (stop - start) / step
        if (stop - start) % step > 0: size += 1
        if w_item.len != size or step < 0:
            w_lst = array_tolist__Array(space, self)
            w_item = space.call_method(w_item, 'tolist')
            space.setitem(w_lst, w_idx, w_item)
            self.setlen(0)
            self.fromsequence(w_lst)
        else:
            for j,i in enumerate(range(start, stop, step)):
                self.buffer[i]=w_item.buffer[j]

    def setslice__Array_ANY_ANY_ANY(space, self, w_i, w_j, w_x):
        space.setitem(self, space.newslice(w_i, w_j, space.w_None), w_x)
    
    
    def array_append__Array_ANY(space, self, w_x):
        x = self.item_w(w_x)
        self.setlen(self.len + 1)
        self.buffer[self.len - 1] = x


    def array_extend__Array_ANY(space, self, w_iterable):
        if isinstance(w_iterable, W_Array):
            oldlen = self.len
            new = w_iterable.len
            self.setlen(self.len + new)
            for i in range(new):
                if oldlen + i >= self.len:
                    self.setlen(oldlen + i + 1)
                self.buffer[oldlen + i] = w_iterable.buffer[i]
            self.setlen(oldlen + i + 1)
        elif isinstance(w_iterable, W_ArrayBase):
            msg = "can only extend with array of same kind"
            raise OperationError(space.w_TypeError, space.wrap(msg))
        else:
            self.fromsequence(w_iterable)


    # Convertions
    
    def array_tolist__Array(space, self):
        w_l=space.newlist([])
        for i in range(self.len):
            w_l.append(getitem__Array_ANY(space, self, space.wrap(i)))
        return w_l

    def array_fromlist__Array_List(space, self, w_lst):
        s = self.len
        try:
            self.fromsequence(w_lst)
        except OperationError:
            self.setlen(s)
            raise

    def array_fromstring__Array_ANY(space, self, w_s):
        s = space.str_w(w_s)
        if len(s)%mytype.bytes !=0:
            msg = 'string length not a multiple of item size'
            raise OperationError(self.space.w_ValueError, self.space.wrap(msg))
        oldlen = self.len
        new = len(s) / mytype.bytes
        self.setlen(oldlen + new)
        cbuf =self.charbuf()
        for i in range(len(s)):
            cbuf[oldlen * mytype.bytes + i] = s[i]

    def array_tostring__Array(space, self):
        cbuf = self.charbuf()
        s = ''
        i=0
        while i < self.len * mytype.bytes:
            s += cbuf[i]
            i+=1
        return self.space.wrap(s)

            

    def array_fromfile__Array_ANY_ANY(space, self, w_f, w_n):
        if space.type(w_f).name != 'file': # FIXME: this cant be the right way?
            msg = "arg1 must be open file"
            raise OperationError(space.w_TypeError, space.wrap(msg))
        n = space.int_w(w_n)
        
        size = self.itemsize * n
        w_item = space.call_method(w_f, 'read', space.wrap(size))
        item = space.str_w(w_item)
        if len(item) < size:
            n = len(item) % self.itemsize
            if n != 0: item = item[0:-(len(item) % self.itemsize)]
            w_item = space.wrap(item)
            array_fromstring__Array_ANY(space, self, w_item)
            msg = "not enough items in file"
            raise OperationError(space.w_EOFError, space.wrap(msg))
        array_fromstring__Array_ANY(space, self, w_item)

    if mytype.typecode == 'u':
        def array_fromunicode__Array_Unicode(space, self, w_ustr):
            # XXX the following probable bug is not emulated:
            # CPython accepts a non-unicode string or a buffer, and then
            # behaves just like fromstring(), except that it strangely truncate
            # string arguments at multiples of the unicode byte size.
            # Let's only accept unicode arguments for now.
            self.fromsequence(w_ustr)
    else:
        def array_fromunicode__Array_Unicode(space, self, w_ustr):
            msg = "fromunicode() may only be called on type 'u' arrays"
            raise OperationError(space.w_ValueError, space.wrap(msg))
        

            
        

    def cmp__Array_ANY(space, self, other):
        if isinstance(other, W_ArrayBase):
            w_lst1 = array_tolist__Array(space, self)
            w_lst2 = array_tolist__Array(space, other)
            return space.cmp(w_lst1, w_lst2)
        else:
            raise OperationError(space.w_NotImplementedError, space.wrap(''))
        

    def repr__Array(space, self):
        if self.len == 0:
            return space.wrap("array('%s')" % self.typecode)
        elif self.typecode == "c":
            r = space.repr(space.call_method(self, 'tostring'))
            s = "array('%s', %s)" % (self.typecode, space.str_w(r))
            return space.wrap(s)
        elif self.typecode == "u":
            r = space.repr(space.call_method(self, 'tounicode'))
            s = "array('%s', %s)" % (self.typecode, space.str_w(r))
            return space.wrap(s)
        else:
            r = space.repr(space.call_method(self, 'tolist'))
            s = "array('%s', %s)" % (self.typecode, space.str_w(r))
            return space.wrap(s)    


    W_Array.__name__ = 'W_ArrayType_'+mytype.typecode
    mytype.w_class = W_Array

    from pypy.objspace.std.sliceobject import W_SliceObject    
    from pypy.objspace.std.listobject import W_ListObject
    from pypy.objspace.std.unicodeobject import W_UnicodeObject
    register_all(locals(), globals())


for mytype in types.values():
    make_array(mytype)
