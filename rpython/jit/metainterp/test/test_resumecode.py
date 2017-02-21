import sys
import pytest
from rpython.jit.metainterp.resumecode import create_numbering,\
    unpack_numbering, Reader, Writer, TagOverflow
from rpython.rtyper.lltypesystem import lltype

from hypothesis import strategies, given, example

def test_tag_overflow():
    w = Writer()
    for bad in [2 ** 21, -2**22, -2**21-1,
                sys.maxint, -sys.maxint-1]:
        with pytest.raises(TagOverflow):
            w.append_int(bad)

examples = [
    [1, 2, 3, 4, 257, 10000, 13, 15],
    [1, 2, 3, 4],
    range(1, 10, 2),
    [13000, 12000, 10000, 256, 255, 254, 257, -3, -1000]
]

codelists = strategies.lists(strategies.integers(-2**21, 2**21-1), min_size=1)

def hypothesis_and_examples(func):
    func = given(codelists)(func)
    for ex in examples:
        func = example(ex)(func)
    return func

@hypothesis_and_examples
def test_roundtrip(l):
    n = create_numbering(l)
    assert unpack_numbering(n) == l

@hypothesis_and_examples
def test_compressing(l):
    n = create_numbering(l)
    assert len(n.code) <= len(l) * 3

@hypothesis_and_examples
def test_reader(l):
    n = create_numbering(l)
    r = Reader(n)
    for i, elt in enumerate(l):
        assert r.items_read == i
        item = r.next_item()
        assert elt == item

@hypothesis_and_examples
def test_writer(l):
    for size in [len(l), 0]:
        w = Writer(len(l))
        for num in l:
            w.append_int(num)
        n = w.create_numbering()
        assert unpack_numbering(n) == l

@hypothesis_and_examples
def test_patch_current_size(l):
    for middle in range(len(l)):
        l1 = l[:middle]
        l2 = l[middle:]
        w = Writer(len(l))
        w.append_int(0)
        for num in l1:
            w.append_int(num)
        w.patch_current_size(0)
        for num in l2:
            w.append_int(num)
        n = w.create_numbering()
        assert unpack_numbering(n)[1:] == l
        assert unpack_numbering(n)[0] == middle + 1

@hypothesis_and_examples
def test_patch(l):
    item = l[0]
    l = l[1:]
    for middle in range(len(l)):
        output = l[:]
        output[middle] = item
        w = Writer(len(l))
        for i, num in enumerate(l):
            index = w.append_int(num)
            assert index == i
        w.patch(middle, item)
        n = w.create_numbering()
        assert unpack_numbering(n) == output

