import typing as tp
from itertools import zip_longest
from copy import deepcopy
from collections import Counter

import numpy as np

from automap import AutoMap
from automap import FrozenAutoMap
from arraykit import immutable_filter
from arraykit import mloc
from arraykit import name_filter
from arraykit import resolve_dtype


from static_frame.core.container import ContainerOperand
from static_frame.core.container_util import apply_binary_operator
from static_frame.core.container_util import matmul
from static_frame.core.container_util import key_from_container_key
from static_frame.core.container_util import sort_index_for_order

from static_frame.core.display import Display
from static_frame.core.display import DisplayActive
from static_frame.core.display_config import DisplayConfig
from static_frame.core.display import DisplayHeader
from static_frame.core.doc_str import doc_inject
from static_frame.core.exception import ErrorInitIndex
from static_frame.core.exception import ErrorInitIndexNonUnique
from static_frame.core.exception import LocInvalid
from static_frame.core.index_base import IndexBase
from static_frame.core.node_dt import InterfaceDatetime
from static_frame.core.node_iter import IterNodeApplyType
from static_frame.core.node_iter import IterNodeDepthLevel
from static_frame.core.node_iter import IterNodeType
from static_frame.core.node_selector import InterfaceGetItem
from static_frame.core.node_selector import InterfaceSelectDuo
from static_frame.core.node_selector import TContainer
from static_frame.core.node_str import InterfaceString
from static_frame.core.node_re import InterfaceRe

from static_frame.core.util import array_shift
from static_frame.core.util import array_sample
from static_frame.core.util import arrays_equal
from static_frame.core.util import array2d_to_tuples
from static_frame.core.util import concat_resolved

from static_frame.core.util import dtype_from_element
from static_frame.core.util import DEFAULT_SORT_KIND
from static_frame.core.util import DepthLevelSpecifier
from static_frame.core.util import DTYPE_DATETIME_KIND
from static_frame.core.util import DTYPE_OBJECTABLE_KINDS
from static_frame.core.util import DTYPE_INT_DEFAULT
from static_frame.core.util import DTYPE_BOOL
from static_frame.core.util import DtypeSpecifier
from static_frame.core.util import EMPTY_ARRAY
from static_frame.core.util import GetItemKeyType
from static_frame.core.util import IndexInitializer
from static_frame.core.util import INT_TYPES
from static_frame.core.util import intersect1d
from static_frame.core.util import isin
from static_frame.core.util import isna_array
from static_frame.core.util import isfalsy_array
from static_frame.core.util import iterable_to_array_1d
from static_frame.core.util import KEY_ITERABLE_TYPES
from static_frame.core.util import KeyIterableTypes
from static_frame.core.util import KeyTransformType
from static_frame.core.util import NAME_DEFAULT
from static_frame.core.util import NameType
from static_frame.core.util import NULL_SLICE
from static_frame.core.util import setdiff1d
from static_frame.core.util import slice_to_inclusive_slice
from static_frame.core.util import to_datetime64
from static_frame.core.util import UFunc
from static_frame.core.util import array_ufunc_axis_skipna
from static_frame.core.util import union1d
from static_frame.core.util import argsort_array
from static_frame.core.util import ufunc_unique1d_indexer
from static_frame.core.util import PositionsAllocator
from static_frame.core.util import array_deepcopy
from static_frame.core.util import DTYPE_OBJECT
from static_frame.core.util import IndexConstructor
from static_frame.core.util import DTYPE_NA_KINDS

from static_frame.core.style_config import StyleConfig
from static_frame.core.loc_map import LocMap


if tp.TYPE_CHECKING:
    import pandas #pylint: disable=W0611 #pragma: no cover
    from static_frame import Series #pylint: disable=W0611 #pragma: no cover
    from static_frame import IndexHierarchy #pylint: disable=W0611 #pragma: no cover
    from static_frame.core.index_auto import RelabelInput #pylint: disable=W0611 #pragma: no cover

I = tp.TypeVar('I', bound=IndexBase)


class ILocMeta(type):

    def __getitem__(cls,
            key: GetItemKeyType
            ) -> 'ILoc':
        return cls(key) #type: ignore

class ILoc(metaclass=ILocMeta):
    '''A wrapper for embedding ``iloc`` specifications within a single axis argument of a ``loc`` selection.
    '''

    STATIC = True
    __slots__ = (
            'key',
            )

    def __init__(self, key: GetItemKeyType):
        self.key = key


def immutable_index_filter(index: I) -> IndexBase:
    '''Return an immutable index. All index objects handle converting from mutable to immutable via the __init__ constructor; but need to use appropriate class between Index and IndexHierarchy.'''

    if index.STATIC:
        return index
    return index._IMMUTABLE_CONSTRUCTOR(index)


def mutable_immutable_index_filter(
        target_static: bool,
        index: I
        ) -> IndexBase:
    if target_static:
        return immutable_index_filter(index)
    # target mutable
    if index.STATIC:
        return index._MUTABLE_CONSTRUCTOR(index)
    return index.__class__(index) # create new instance

#-------------------------------------------------------------------------------
_INDEX_SLOTS = (
        '_map',
        '_labels',
        '_positions',
        '_recache',
        '_name'
        )

class Index(IndexBase):
    '''A mapping of labels to positions, immutable and of fixed size. Used by default in :obj:`Series` and as index and columns in :obj:`Frame`. Base class of all 1D indices.'''

    __slots__ = _INDEX_SLOTS

    # _IMMUTABLE_CONSTRUCTOR is None from IndexBase
    # _MUTABLE_CONSTRUCTOR will be set after IndexGO defined

    _UFUNC_UNION = union1d
    _UFUNC_INTERSECTION = intersect1d
    _UFUNC_DIFFERENCE = setdiff1d

    _DTYPE: tp.Optional[np.dtype] = None # for specialized indices requiring a typed labels

    # for compatability with IndexHierarchy, where this is implemented as a property method
    depth: int = 1

    _map: tp.Optional[FrozenAutoMap]
    _labels: np.ndarray
    _positions: np.ndarray
    _recache: bool
    _name: NameType

    #---------------------------------------------------------------------------
    # methods used in __init__ that are customized in derived classes; there, we need to mutate instance state, this these are instance methods
    @staticmethod
    def _extract_labels(
            mapping: tp.Optional[tp.Dict[tp.Hashable, int]],
            labels: tp.Iterable[tp.Hashable],
            dtype: tp.Optional[np.dtype] = None
            ) -> np.ndarray:
        '''Derive labels, a cache of the mapping keys in a sequence type (either an ndarray or a list).

        If the labels passed at instantiation are an ndarray, they are used after immutable filtering. Otherwise, the mapping keys are used to create an ndarray.

        This method is overridden in the derived class.

        Args:
            mapping: Can be None if loc_is_iloc.
            labels: might be an expired Generator, but if it is an immutable ndarray, we can use it without a copy.
        '''
        # pre-fetching labels for faster get_item construction
        if labels.__class__ is np.ndarray:
            if dtype is not None and dtype != labels.dtype: #type: ignore
                raise ErrorInitIndex('invalid label dtype for this Index')
            return immutable_filter(labels)

        # labels may be an expired generator, must use the mapping
        labels_src = labels if hasattr(labels, '__len__') else mapping

        if len(labels_src) == 0: #type: ignore
            if dtype is None:
                labels = EMPTY_ARRAY
            else:
                labels = np.empty(0, dtype=dtype)
                labels.flags.writeable = False #type: ignore
        else: # resolving the dtype is expensive, pass if possible
            labels, _ = iterable_to_array_1d(labels_src, dtype=dtype) #type: ignore

        return labels

    @staticmethod
    def _extract_positions(
            size: int,
            positions: tp.Optional[tp.Sequence[int]]
            ) -> np.ndarray:
        # positions is either None or an ndarray
        if positions.__class__ is np.ndarray:
            return immutable_filter(positions)
        return PositionsAllocator.get(size)

    #---------------------------------------------------------------------------
    # constructors

    @classmethod
    def from_labels(cls: tp.Type[I],
            labels: tp.Iterable[tp.Sequence[tp.Hashable]],
            *,
            name: NameType = None
            ) -> I:
        '''
        Construct an ``Index`` from an iterable of labels, where each label is a hashable. Provided for a compatible interface to ``IndexHierarchy``.
        '''
        return cls(labels, name=name)


    @staticmethod
    def _error_init_index_non_unique(labels: tp.Iterable[tp.Hashable]) -> ErrorInitIndexNonUnique:
        labels_counter = Counter(labels)
        if len(labels_counter) == 0: # generator consumed
            msg = 'Labels have non-unique values. Details from iterators not available.'
        else:
            labels_all = sum(labels_counter.values())
            labels_duplicated = [repr(p[0]) for p in labels_counter.most_common(10) if p[1] > 1]
            msg = f'Labels have {labels_all - len(labels_counter)} non-unique values, including {", ".join(labels_duplicated)}.'

        return ErrorInitIndexNonUnique(msg)

    #---------------------------------------------------------------------------
    @doc_inject(selector='index_init')
    def __init__(self,
            labels: IndexInitializer,
            *,
            loc_is_iloc: bool = False,
            name: NameType = NAME_DEFAULT,
            dtype: DtypeSpecifier = None,
            ) -> None:
        '''Initializer.

        {args}
        '''
        self._recache: bool = False
        self._map: tp.Optional[FrozenAutoMap] = None

        positions = None
        is_typed = self._DTYPE is not None # only True for datetime64 indices

        # resolve the targetted labels dtype, by lookin at the class attr _DTYPE and/or the passed dtype argument
        if dtype is None:
            dtype_extract = self._DTYPE # set in some specialized Index sub-classes
        else: # passed dtype is not None
            if is_typed and dtype != self._DTYPE:
                # NOTE: should never get to this branch, as derived Index classes that set _DTYPE remove dtype from __init__
                raise ErrorInitIndex('invalid dtype argument for this Index', dtype, self._DTYPE) #pragma: no cover
            # self._DTYPE is None, passed dtype is not None, use dtype
            dtype_extract = dtype

        #-----------------------------------------------------------------------
        # handle all Index subclasses
        if labels.__class__ is np.ndarray:
            pass
        elif isinstance(labels, IndexBase):
            if labels._recache:
                labels._update_array_cache()
            if name is NAME_DEFAULT:
                name = labels.name # immutable, so no copy necessary
            if isinstance(labels, Index): # not an IndexHierarchy
                if (labels.STATIC and self.STATIC and dtype is None):
                    if not is_typed or (is_typed and self._DTYPE == labels.dtype):
                        # can take the map if static and if types in the dict are the same as those in the labels (or to become the labels after conversion)
                        self._map = labels._map
                # get a reference to the immutable arrays, even if this is an IndexGO index, we can take the cached arrays, assuming they are up to date; for datetime64 indices, we might need to translate to a different type
                positions = labels._positions
                loc_is_iloc = labels._map is None
                labels = labels._labels
            else: # IndexHierarchy
                # will be a generator of tuples; already updated caches
                labels = labels.__iter__()
        elif isinstance(labels, ContainerOperand):
            # it is a Series or similar
            array = labels.values
            if array.ndim == 1:
                labels = array
            else:
                labels = array2d_to_tuples(array)
        # else: assume an iterable suitable for labels usage, we will identify strings later

        #-----------------------------------------------------------------------
        if is_typed:
            # do not need to check arrays, as will and checked to match dtype_extract in _extract_labels
            if not labels.__class__ is np.ndarray:
                # for now, assume that if _DTYPE is defined, we have a date
                labels = (to_datetime64(v, dtype_extract) for v in labels)
            # coerce to target type
            elif labels.dtype != dtype_extract: #type: ignore
                labels = labels.astype(dtype_extract) #type: ignore
                labels.flags.writeable = False #type: ignore
            labels_for_automap = labels

        self._name = None if name is NAME_DEFAULT else name_filter(name)

        if self._map is None: # if _map not shared from another Index
            if not loc_is_iloc:
                # PERF: calling tolist before initializing AutoMap is shown to be about 2x faster, but can only be done with NumPy dtypes that are equivalent after conversion to Python objects
                if not is_typed:
                    if labels.__class__ is np.ndarray and labels.dtype.kind in DTYPE_OBJECTABLE_KINDS: #type: ignore
                        labels_for_automap = labels.tolist() #type: ignore
                    elif isinstance(labels, str):
                        # NOTE: this is necessary as otherwise a malformed Index will be created, whereby the _map will treat the string as an iterable of chars, while the labels will not and have a single string value. This is consisten as other elements (ints, Booleans) are rejected on instantiation of the AutoMap
                        raise ErrorInitIndex('Cannot create an Index from a single string; provide an iterable of strings.')
                    else:
                        labels_for_automap = labels
                else:
                    labels_for_automap = labels
                try:
                    self._map = FrozenAutoMap(labels_for_automap) if self.STATIC else AutoMap(labels_for_automap)
                except ValueError: # Automap will raise ValueError of non-unique values are encountered
                    raise self._error_init_index_non_unique(labels_for_automap) from None
                size = len(self._map)
            else:
                # if loc_is_iloc, labels must be positions and we assume that internal clients that provided loc_is_iloc will not give a generator
                size = len(labels) #type: ignore
                if positions is None:
                    positions = labels
        else: # map shared from another Index
            size = len(self._map)

        # this might be NP array, or a list, depending on if static or grow only; if an array, dtype will be compared with passed dtype_extract
        self._labels = self._extract_labels(self._map, labels, dtype_extract)
        self._positions = self._extract_positions(size, positions)

        if self._DTYPE and self._labels.dtype != self._DTYPE:
            raise ErrorInitIndex('Invalid label dtype for this Index', #pragma: no cover
                    self._labels.dtype, self._DTYPE)

        # NOTE: to implement GH # 374; do this after final self._labels creation as user may pass a dtype argument
        if not is_typed and self._labels.dtype.kind == DTYPE_DATETIME_KIND:
            raise ErrorInitIndex('Cannot create an Index with a datetime64 array; use an Index subclass (e.g. IndexDate) or supply an `index_constructors` argument')

    #---------------------------------------------------------------------------

    def __setstate__(self, state: tp.Tuple[None, tp.Dict[str, tp.Any]]) -> None:
        '''
        Ensure that reanimated NP arrays are set not writeable.
        '''
        for key, value in state[1].items():
            setattr(self, key, value)
        self._labels.flags.writeable = False

    def __deepcopy__(self: I, memo: tp.Dict[int, tp.Any]) -> I:
        assert not self._recache # __deepcopy__ is implemented on derived GO class
        obj = self.__new__(self.__class__)
        obj._map = deepcopy(self._map, memo) #type: ignore
        obj._labels = array_deepcopy(self._labels, memo) #type: ignore
        obj._positions = PositionsAllocator.get(len(self._labels)) #type: ignore
        obj._recache = False
        obj._name = self._name # should be hashable/immutable

        memo[id(self)] = obj
        return obj #type: ignore

    def __copy__(self: I) -> I:
        '''
        Return shallow copy of this Index.
        '''
        if self._recache:
            self._update_array_cache()

        return self.__class__(self, name=self._name)

    def copy(self: I) -> I:
        '''
        Return shallow copy of this Index.
        '''
        return self.__copy__() #type: ignore

    #---------------------------------------------------------------------------
    # name interface

    def rename(self: I, name: NameType) -> I:
        '''
        Return a new Frame with an updated name attribute.
        '''
        if self._recache:
            self._update_array_cache()
        # let the constructor handle reuse
        return self.__class__(self, name=name)

    #---------------------------------------------------------------------------
    # interfaces

    @property
    def loc(self) -> InterfaceGetItem[TContainer]:
        return InterfaceGetItem(self._extract_loc) #type: ignore

    @property
    def iloc(self) -> InterfaceGetItem[TContainer]:
        return InterfaceGetItem(self._extract_iloc) #type: ignore

    # # on Index, getitem is an iloc selector; on Series, getitem is a loc selector; for this extraction interface, we do not implement a getitem level function (using iloc would be consistent), as it is better to be explicit between iloc loc

    def _iter_label(self,
            depth_level: tp.Optional[DepthLevelSpecifier] = None
            ) -> tp.Iterator[tp.Hashable]:
        yield from self._labels

    def _iter_label_items(self,
            depth_level: tp.Optional[DepthLevelSpecifier] = None
            ) -> tp.Iterator[tp.Tuple[int, tp.Hashable]]:
        yield from zip(self._positions, self._labels)

    @property
    def iter_label(self) -> IterNodeDepthLevel[tp.Any]:
        return IterNodeDepthLevel(
                container=self,
                function_items=self._iter_label_items,
                function_values=self._iter_label,
                yield_type=IterNodeType.VALUES,
                apply_type=IterNodeApplyType.INDEX_LABELS
                )


    #---------------------------------------------------------------------------
    # common attributes from the numpy array

    @property # type: ignore
    @doc_inject()
    def mloc(self) -> int:
        '''{doc_int}
        '''
        if self._recache:
            self._update_array_cache()
        return mloc(self._labels)

    @property
    def dtype(self) -> np.dtype:
        '''
        Return the dtype of the underlying NumPy array.

        Returns:
            :obj:`numpy.dtype`
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels.dtype

    @property
    def shape(self) -> tp.Tuple[int, ...]:
        '''
        Return a tuple describing the shape of the underlying NumPy array.

        Returns:
            :obj:`tp.Tuple[int]`
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels.shape #type: ignore

    @property
    def ndim(self) -> int:
        '''
        Return the number of dimensions.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels.ndim #type: ignore

    @property
    def size(self) -> int:
        '''
        Return the size of the underlying NumPy array.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels.size #type: ignore

    @property
    def nbytes(self) -> int:
        '''
        Return the total bytes of the underlying NumPy array.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels.nbytes #type: ignore

    #---------------------------------------------------------------------------
    # set operations

    def _ufunc_set(self: I,
            func: tp.Callable[[np.ndarray, np.ndarray, bool], np.ndarray],
            other: tp.Union['IndexBase', tp.Iterable[tp.Hashable]]
            ) -> I:
        '''
        Utility function for preparing and collecting values for Indices to produce a new Index.
        '''
        if self._recache:
            self._update_array_cache()

        if self.equals(other, compare_dtype=True):
            # compare dtype as result should be resolved, even if values are the same
            if (func is self.__class__._UFUNC_INTERSECTION or
                    func is self.__class__._UFUNC_UNION):
                # NOTE: this will delegate name attr
                return self if self.STATIC else self.copy()
            elif func is self.__class__._UFUNC_DIFFERENCE:
                if self._DTYPE is None: #type: ignore
                    # an index with a variable dtype accepts a dtype argument
                    return self.__class__((), dtype=self.dtype) #type: ignore
                # if self._DTYPE is defined, the default constructor does not take a dtype argument
                return self.__class__(())

        if other.__class__ is np.ndarray:
            operand = other
            assume_unique = False
        elif isinstance(other, IndexBase):
            operand = other.values
            assume_unique = True # can always assume unique
        else:
            operand, assume_unique = iterable_to_array_1d(other)

        cls = self.__class__

        # using assume_unique will permit retaining order when operands are identical
        labels = func(self.values, operand, assume_unique=assume_unique) # type: ignore
        if id(labels) == id(self.values):
            # NOTE: favor using cls constructor here as it permits maximal sharing of static resources and the underlying dictionary
            return cls(self)

        return cls.from_labels(labels)


    #---------------------------------------------------------------------------
    def _drop_iloc(self, key: GetItemKeyType) -> 'Index':
        '''Create a new index after removing the values specified by the iloc key.
        '''
        if self._recache:
            self._update_array_cache()

        if key is None:
            if self.STATIC: # immutable, no selection, can return self
                return self
            labels = self._labels # already immutable
        elif key.__class__ is np.ndarray and key.dtype == bool: #type: ignore
            # can use labels, as we already recached
            # use Boolean area to select indices from positions, as np.delete does not work with arrays
            labels = np.delete(self._labels, self._positions[key], axis=0)
            labels.flags.writeable = False
        else:
            labels = np.delete(self._labels, key, axis=0)
            labels.flags.writeable = False

        # from labels will work with both Index and IndexHierarchy
        return self.__class__.from_labels(labels, name=self._name)

    def _drop_loc(self, key: GetItemKeyType) -> 'IndexBase':
        '''Create a new index after removing the values specified by the loc key.
        '''
        return self._drop_iloc(self._loc_to_iloc(key))


    @property
    def drop(self) -> InterfaceSelectDuo[TContainer]:
        return InterfaceSelectDuo( #type: ignore
            func_iloc=self._drop_iloc,
            func_loc=self._drop_loc,
            )


    @doc_inject(select='astype')
    def astype(self, dtype: DtypeSpecifier) -> 'Index':
        '''
        Return an Index with type determined by `dtype` argument. If a `datetime64` dtype is provided, the appropriate ``Index`` subclass will be returned. Note that for Index, this is a simple function, whereas for ``IndexHierarchy``, this is an interface exposing both a callable and a getitem interface.

        Args:
            {dtype}
        '''
        from static_frame.core.index_datetime import dtype_to_index_cls
        array = self.values.astype(dtype)
        cls = dtype_to_index_cls(self.STATIC, array.dtype)
        return cls(
                array,
                name=self._name
                )


    #---------------------------------------------------------------------------
    @property
    def via_str(self) -> InterfaceString[np.ndarray]:
        '''
        Interface for applying string methods to elements in this container.
        '''
        if self._recache:
            self._update_array_cache()

        def blocks_to_container(blocks: tp.Iterator[np.ndarray]) -> np.ndarray:
            return next(blocks)

        return InterfaceString(
                blocks=(self._labels,),
                blocks_to_container=blocks_to_container,
                )

    @property
    def via_dt(self) -> InterfaceDatetime[np.ndarray]:
        '''
        Interface for applying datetime properties and methods to elements in this container.
        '''
        if self._recache:
            self._update_array_cache()

        def blocks_to_container(blocks: tp.Iterator[np.ndarray]) -> np.ndarray:
            return next(blocks)

        return InterfaceDatetime(
                blocks=(self.values,),
                blocks_to_container=blocks_to_container,
                )

    def via_re(self,
            pattern: str,
            flags: int = 0,
            ) -> InterfaceRe[np.ndarray]:
        '''
        Interface for applying regular expressions to elements in this container.
        '''
        if self._recache:
            self._update_array_cache()

        def blocks_to_container(blocks: tp.Iterator[np.ndarray]) -> np.ndarray:
            return next(blocks)

        return InterfaceRe(
                blocks=(self._labels,),
                blocks_to_container=blocks_to_container,
                pattern=pattern,
                flags=flags,
                )

    #---------------------------------------------------------------------------

    def _update_array_cache(self) -> None:
        '''Derived classes can use this to set stored arrays, self._labels and self._positions.
        '''

    #---------------------------------------------------------------------------

    def __len__(self) -> int:
        if self._recache:
            self._update_array_cache()
        return len(self._labels)

    @doc_inject()
    def display(self,
            config: tp.Optional[DisplayConfig] = None,
            *,
            style_config: tp.Optional[StyleConfig] = None,
            ) -> Display:
        '''{doc}

        Args:
            {config}
        '''
        config = config or DisplayActive.get()

        if self._recache:
            self._update_array_cache()

        header: tp.Optional[DisplayHeader]

        if config.type_show:
            header = DisplayHeader(self.__class__, self._name)
            header_depth = 1
        else:
            header = None
            header_depth = 0

        return Display.from_values(self.values,
                header=header,
                config=config,
                outermost=True,
                index_depth=0,
                header_depth=header_depth,
                style_config=style_config,
                )

    #---------------------------------------------------------------------------
    # core internal representation

    @property #type: ignore
    @doc_inject(selector='values_1d', class_name='Index')
    def values(self) -> np.ndarray:
        '''
        {}
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels

    @property
    def positions(self) -> np.ndarray:
        '''Return the immutable positions array.
        '''
        # This is needed by some clients, such as Series and Frame, to support Boolean usage in drop.
        if self._recache:
            self._update_array_cache()
        return self._positions

    def _index_iloc_map(self: I, other: I) -> np.ndarray:
        '''
        Return an array of index locations to map from this array to another

        Equivalent to: self.iter_label().apply(other._loc_to_iloc)
        '''
        if self.__len__() == 0:
            return EMPTY_ARRAY

        ar1 = self.values
        ar2 = other.values

        ar1, ar1_indexer = ufunc_unique1d_indexer(ar1)

        aux = concat_resolved((ar1, ar2))
        aux_sort_indices = argsort_array(aux)
        aux = aux[aux_sort_indices]

        mask = aux[1:] == aux[:-1]

        indexer = aux_sort_indices[1:][mask] - ar1.size

        # We want to return these indices to match ar1 before it was sorted
        try:
            indexer = indexer[ar1_indexer]
        except IndexError:
            # Display the first missing element
            raise KeyError(self.difference(other)[0])

        indexer.flags.writeable = False
        return indexer

    @staticmethod
    def _depth_level_validate(depth_level: DepthLevelSpecifier) -> None:
        '''
        Handle all variety of depth_level specifications for a 1D index: only 0, -1, and lists of the same are valid.
        '''
        if not isinstance(depth_level, INT_TYPES):
            depth_level = tuple(depth_level)
            if len(depth_level) != 1:
                raise RuntimeError('invalid depth_level', depth_level)
            depth_level = depth_level[0]

        if depth_level > 0 or depth_level < -1:
            raise RuntimeError('invalid depth_level', depth_level)

    def values_at_depth(self,
            depth_level: DepthLevelSpecifier = 0
            ) -> np.ndarray:
        '''
        Return an NP array for the `depth_level` specified.
        '''
        self._depth_level_validate(depth_level)
        return self.values

    @doc_inject()
    def label_widths_at_depth(self,
            depth_level: DepthLevelSpecifier = 0
            ) -> tp.Iterator[tp.Tuple[tp.Hashable, int]]:
        '''{}'''
        self._depth_level_validate(depth_level)
        yield from zip_longest(self.values, (), fillvalue=1)

    @property
    def index_types(self) -> 'Series':
        '''
        Return a Series of Index classes for each index depth.

        Returns:
            :obj:`Series`
        '''
        from static_frame.core.series import Series
        return Series((self.__class__,), index=(self._name,), dtype=DTYPE_OBJECT)


    #---------------------------------------------------------------------------

    def relabel(self, mapper: 'RelabelInput') -> 'Index':
        '''
        Return a new Index with labels replaced by the callable or mapping; order will be retained. If a mapping is used, the mapping need not map all origin keys.
        '''
        if self._recache:
            self._update_array_cache()

        if not callable(mapper):
            # if a mapper, it must support both __getitem__ and __contains__
            getitem = getattr(mapper, '__getitem__')
            return self.__class__(
                    (getitem(x) if x in mapper else x for x in self._labels),
                    name=self._name
                    )

        return self.__class__(
                (mapper(x) for x in self._labels),
                name=self._name
                )

    #---------------------------------------------------------------------------
    # extraction and selection

    def _loc_to_iloc(self,
            key: GetItemKeyType,
            key_transform: KeyTransformType = None,
            partial_selection: bool = False,
            ) -> GetItemKeyType:
        '''
        Note: Boolean Series are reindexed to this index, then passed on as all Boolean arrays.

        Args:
            key_transform: A function that transforms keys to specialized type; used by IndexDate indices.
        Returns:
            Return GetItemKey type that is based on integers, compatible with TypeBlocks
        '''
        if key.__class__ is ILoc:
            return key.key #type: ignore

        key = key_from_container_key(self, key)

        if self._map is None: # loc_is_iloc
            if key.__class__ is np.ndarray:
                if key.dtype == bool: #type: ignore
                    return key
                if key.dtype != DTYPE_INT_DEFAULT: #type: ignore
                    # if key is an np.array, it must be an int or bool type
                    # could use tolist(), but we expect all keys to be integers
                    return key.astype(DTYPE_INT_DEFAULT) #type: ignore
            elif key.__class__ is slice:
                key = slice_to_inclusive_slice(key) #type: ignore
            return key

        if key_transform:
            key = key_transform(key)

        # PERF: isolate for usage of _positions
        if self._recache:
            self._update_array_cache()

        return LocMap.loc_to_iloc(
                label_to_pos=self._map,
                labels=self._labels,
                positions=self._positions, # always an np.ndarray
                key=key,
                partial_selection=partial_selection,
                )

    def loc_to_iloc(self,
            key: GetItemKeyType,
            ) -> GetItemKeyType:
        '''Given a label (loc) style key (either a label, a list of labels, a slice, or a Boolean selection), return the index position (iloc) style key. Keys that are not found will raise a KeyError or a sf.LocInvalid error.

        Args:
            key: a label key.
        '''
        if self._map is None: # loc is iloc
            is_bool_array = key.__class__ is np.ndarray and key.dtype == DTYPE_BOOL #type: ignore

            try:
                result = self._positions[key]
            except IndexError:
                # NP gives us: IndexError: only integers, slices (`:`), ellipsis (`...`), numpy.newaxis (`None`) and integer or boolean arrays are valid indices
                if is_bool_array:
                    raise # loc selection on Boolean array selection returns IndexError
                raise KeyError(key)
            except TypeError:
                raise LocInvalid(f'Invalid loc: {key}')

            if is_bool_array:
                return result # return position as array

            if isinstance(key, slice):
                if key == NULL_SLICE:
                    return NULL_SLICE
                if key.stop >= len(self):
                    # while a valid slice of positions, loc lookups do not permit over-stating boundaries
                    raise LocInvalid(f'Invalid loc: {key}')
                key = slice_to_inclusive_slice(key)

            return key

        return self._loc_to_iloc(key)

    def _extract_iloc(self,
            key: GetItemKeyType,
            ) -> tp.Union['Index', tp.Hashable]:
        '''Extract a new index given an iloc key.
        '''
        if self._recache:
            self._update_array_cache()

        if key is None:
            labels = self._labels
            loc_is_iloc = self._map is None
        elif key.__class__ is slice:
            if key == NULL_SLICE:
                labels = self._labels
                loc_is_iloc = self._map is None
            else:
                # if labels is an np array, this will be a view; if a list, a copy
                labels = self._labels[key]
                labels.flags.writeable = False
                loc_is_iloc = False
        elif isinstance(key, KEY_ITERABLE_TYPES):
            # we assume Booleans have been normalized to integers here
            # can select directly from _labels[key] if if key is a list
            labels = self._labels[key]
            labels.flags.writeable = False
            loc_is_iloc = False
        else: # select a single label value
            return self._labels[key] #type: ignore

        return self.__class__(labels=labels,
                loc_is_iloc=loc_is_iloc,
                name=self._name,
                )

    def _extract_iloc_by_int(self,
            key: int,
            ) -> tp.Hashable:
        '''Extract an element given an iloc integer key.
        '''
        if self._recache:
            self._update_array_cache()
        return self._labels[key] #type: ignore

    def _extract_loc(self: I,
            key: GetItemKeyType
            ) -> tp.Union['Index', tp.Hashable]:
        return self._extract_iloc(self._loc_to_iloc(key))

    def __getitem__(self: I,
            key: GetItemKeyType
            ) -> tp.Union['Index', tp.Hashable]:
        '''Extract a new index given an iloc key.
        '''
        return self._extract_iloc(key)

    #---------------------------------------------------------------------------
    # operators

    def _ufunc_unary_operator(self,
            operator: UFunc
            ) -> np.ndarray:
        '''Always return an NP array.
        '''
        if self._recache:
            self._update_array_cache()

        array = operator(self._labels)
        array.flags.writeable = False
        return array

    def _ufunc_binary_operator(self, *,
            operator: UFunc,
            other: tp.Any,
            fill_value: object = np.nan,
            ) -> np.ndarray:
        '''
        Binary operators applied to an index always return an NP array. This deviates from Pandas, where some operations (multiplying an int index by an int) result in a new Index, while other operations result in a np.array (using == on two Index).
        '''
        from static_frame.core.series import Series
        from static_frame.core.frame import Frame

        if self._recache:
            self._update_array_cache()

        if isinstance(other, (Series, Frame)):
            raise ValueError('cannot use labelled container as an operand.')

        values = self._labels
        other_is_array = False

        if issubclass(other.__class__, Index):
            other = other.values # operate on labels to labels
            other_is_array = True
        elif other.__class__ is np.ndarray:
            other_is_array = True

        if operator.__name__ == 'matmul':
            return matmul(values, other)
        elif operator.__name__ == 'rmatmul':
            return matmul(other, values)

        return apply_binary_operator(
                values=values,
                other=other,
                other_is_array=other_is_array,
                operator=operator,
                )

    def _ufunc_axis_skipna(self, *,
            axis: int,
            skipna: bool,
            ufunc: UFunc,
            ufunc_skipna: UFunc,
            composable: bool,
            dtypes: tp.Tuple[np.dtype, ...],
            size_one_unity: bool
            ) -> np.ndarray:
        '''

        Args:
            dtype: Not used in 1D application, but collected here to provide a uniform signature.
        '''
        if self._recache:
            self._update_array_cache()

        # do not need to pass on composabel here
        return array_ufunc_axis_skipna(
                array=self._labels,
                skipna=skipna,
                axis=0,
                ufunc=ufunc,
                ufunc_skipna=ufunc_skipna
                )

    # _ufunc_shape_skipna defined in IndexBase

    #---------------------------------------------------------------------------
    # dictionary-like interface

    # NOTE: we intentionally exclude keys(), items(), and get() from Index classes, as they return inconsistent result when thought of as a dictionary


    def __iter__(self) -> tp.Iterator[tp.Hashable]:
        '''Iterate over labels.
        '''
        if self._recache:
            self._update_array_cache()
        yield from self._labels.__iter__()

    def __reversed__(self) -> tp.Iterator[tp.Hashable]:
        '''
        Returns a reverse iterator on the index labels.
        '''
        if self._recache:
            self._update_array_cache()
        return reversed(self._labels)

    def __contains__(self, value: tp.Any) -> bool:
        '''Return True if value in the labels.
        '''
        if self._map is None: # loc_is_iloc
            if isinstance(value, INT_TYPES):
                return value >= 0 and value < len(self) #type: ignore
            return False #type: ignore [unreachable]
        return self._map.__contains__(value) #type: ignore


    #---------------------------------------------------------------------------
    # utility functions

    def unique(self,
            depth_level: DepthLevelSpecifier = 0
            ) -> np.ndarray:
        '''
        Return a NumPy array of unique values.

        Args:
            depth_level: defaults to 0 for for a 1D Index.

        Returns:
            :obj:`numpy.ndarray`
        '''
        self._depth_level_validate(depth_level)
        return self.values

    @doc_inject()
    def equals(self,
            other: tp.Any,
            *,
            compare_name: bool = False,
            compare_dtype: bool = False,
            compare_class: bool = False,
            skipna: bool = True,
            ) -> bool:
        '''
        {doc}

        Args:
            {compare_name}
            {compare_dtype}
            {compare_class}
            {skipna}
        '''

        if id(other) == id(self):
            return True

        if compare_class and self.__class__ != other.__class__:
            return False
        elif not isinstance(other, Index):
            return False

        # defer updating cache
        if self._recache:
            self._update_array_cache()

        # same type from here
        if len(self) != len(other):
            return False
        if compare_name and self.name != other.name:
            return False
        if compare_dtype and self.dtype != other.dtype:
            return False
        return arrays_equal(self.values, other.values, skipna=skipna)


    @doc_inject(selector='sort')
    def sort(self,
            ascending: bool = True,
            kind: str = DEFAULT_SORT_KIND,
            key: tp.Optional[tp.Callable[['Index'], tp.Union[np.ndarray, 'Index']]] = None,
            ) -> 'Index':
        '''Return a new Index with the labels sorted.

        Args:
            {ascending}
            {kind}
            {key}
        '''
        order = sort_index_for_order(self, kind=kind, ascending=ascending, key=key) #type: ignore [arg-type]

        return self._extract_iloc(order) #type: ignore [return-value]

    def isin(self, other: tp.Iterable[tp.Any]) -> np.ndarray:
        '''
        Return a Boolean array showing True where a label is found in other. If other is a multidimensional array, it is flattened.
        '''
        return isin(self.values, other, array_is_unique=True)

    def roll(self, shift: int) -> 'Index':
        '''Return an Index with values rotated forward and wrapped around (with a postive shift) or backward and wrapped around (with a negative shift).
        '''
        values = self.values # force usage of property for cache update
        if shift % len(values):
            values = array_shift(
                    array=values,
                    shift=shift,
                    axis=0,
                    wrap=True)
            values.flags.writeable = False
        return self.__class__(values, name=self._name)

    #---------------------------------------------------------------------------
    # na handling
    # falsy handling

    def _drop_missing(self,
            func: tp.Callable[[np.ndarray], np.ndarray],
            dtype_kind_targets: tp.Optional[tp.FrozenSet[str]],
            ) -> 'Index':
        '''
        Args:
            func: UFunc that returns True for missing values
        '''
        labels = self.values
        if dtype_kind_targets is not None and labels.dtype.kind not in dtype_kind_targets:
            return self if self.STATIC else self.copy()

        # get positions that we want to keep
        isna = func(labels)
        length = len(labels)
        count = isna.sum()

        if count == length: # all are NaN
            return self.__class__((), name=self.name)
        if count == 0: # None are nan
            return self if self.STATIC else self.copy()

        sel = np.logical_not(isna)
        values = labels[sel]
        values.flags.writeable = False

        return self.__class__(values,
                name=self._name,
                )

    def dropna(self) -> 'Index':
        '''
        Return a new :obj:`Index` after removing values of NaN or None.
        '''
        return self._drop_missing(isna_array, DTYPE_NA_KINDS)

    def dropfalsy(self) -> 'Index':
        '''
        Return a new :obj:`Index` after removing values of NaN or None.
        '''
        return self._drop_missing(isfalsy_array, None)

    #---------------------------------------------------------------------------

    def _fill_missing(self,
            func: tp.Callable[[np.ndarray], np.ndarray],
            value: tp.Any,
            ) -> 'Index':
        values = self.values # force usage of property for cache update
        sel = func(values)
        if not np.any(sel):
            return self if self.STATIC else self.copy()

        value_dtype = dtype_from_element(value)
        assignable_dtype = resolve_dtype(value_dtype, values.dtype)

        if values.dtype == assignable_dtype:
            assigned = values.copy()
        else:
            assigned = values.astype(assignable_dtype)

        assigned[sel] = value
        assigned.flags.writeable = False
        return self.__class__(assigned, name=self._name)

    @doc_inject(selector='fillna')
    def fillna(self, value: tp.Any) -> 'Index':
        '''Return an :obj:`Index` with replacing null (NaN or None) with the supplied value.

        Args:
            {value}
        '''
        return self._fill_missing(isna_array, value)

    @doc_inject(selector='fillna')
    def fillfalsy(self, value: tp.Any) -> 'Index':
        '''Return an :obj:`Index` with replacing falsy values with the supplied value.

        Args:
            {value}
        '''
        return self._fill_missing(isfalsy_array, value)

    #---------------------------------------------------------------------------
    def _sample_and_key(self,
            count: int = 1,
            *,
            seed: tp.Optional[int] = None,
            ) -> tp.Tuple['Index', np.ndarray]:
        # NOTE: base class defines pubic method
        # force usage of property for cache update
        # sort positions to avoid uncomparable objects
        key = array_sample(self.positions, count=count, seed=seed, sort=True)

        values = self.values[key]
        values.flags.writeable = False
        return self.__class__(values, name=self._name), key


    #---------------------------------------------------------------------------
    # export

    def to_series(self) -> 'Series':
        '''Return a Series with values from this Index's labels.
        '''
        # NOTE: while we might re-use the index on the index returned from this Series, such an approach will not work with IndexHierarchy.to_frame, as we do not know if the index should be on the index or columns; thus, returning an unindexed Series is appropriate
        from static_frame import Series
        return Series(self.values, name=self._name)


    def level_add(self,
            level: tp.Hashable,
            *,
            index_constructor: IndexConstructor = None,
            ) -> 'IndexHierarchy':
        '''Return an IndexHierarchy with an added root level.

        Args:
            level: A hashable to used as the new root.
            *,
            index_constructor:
        '''
        from static_frame import IndexHierarchy
        from static_frame import IndexHierarchyGO
        from static_frame import Index
        from static_frame import IndexGO

        cls = IndexHierarchy if self.STATIC else IndexHierarchyGO
        cls_depth = Index if self.STATIC else IndexGO

        if index_constructor is None:
            # cannot assume new depth is the same index subclass
            index_constructor = cls_depth

        indices = [index_constructor((level,)), immutable_index_filter(self)]

        indexers = np.array(
                [
                    np.zeros(self.__len__(), dtype=DTYPE_INT_DEFAULT),
                    PositionsAllocator.get(self.__len__())
                ]
        )
        indexers.flags.writeable = False

        return cls(
                indices=indices,
                indexers=indexers,
                name=self._name,
                )

    def to_pandas(self) -> 'pandas.Index':
        '''Return a Pandas Index.
        '''
        import pandas
        # must copy to remove immutability, decouple reference
        if self._map is None:
            return pandas.RangeIndex(self.__len__(), name=self._name)
        return pandas.Index(self.values.copy(),
                name=self._name)

#-------------------------------------------------------------------------------
_INDEX_GO_SLOTS = (
        '_map',
        '_labels',
        '_positions',
        '_recache',
        '_name',
        '_labels_mutable',
        '_labels_mutable_dtype',
        '_positions_mutable_count',
        )


class _IndexGOMixin:

    STATIC = False
    # NOTE: must define in derived class or get TypeError: multiple bases have instance lay-out conflict
    __slots__ = ()

    _map: tp.Optional[AutoMap]
    _labels: np.ndarray
    _positions: np.ndarray
    _labels_mutable: tp.List[tp.Hashable]
    _labels_mutable_dtype: np.dtype
    _positions_mutable_count: int

    #---------------------------------------------------------------------------
    def __deepcopy__(self: I, memo: tp.Dict[int, tp.Any]) -> I: #type: ignore
        if self._recache:
            self._update_array_cache()

        obj = self.__new__(self.__class__)
        obj._map = deepcopy(self._map, memo) #type: ignore
        obj._labels = array_deepcopy(self._labels, memo) #type: ignore
        obj._positions = PositionsAllocator.get(len(self._labels)) #type: ignore
        obj._recache = False
        obj._name = self._name # should be hashable/immutable
        obj._labels_mutable = deepcopy(self._labels_mutable, memo) #type: ignore
        obj._labels_mutable_dtype = deepcopy(self._labels_mutable_dtype, memo) #type: ignore
        obj._positions_mutable_count = self._positions_mutable_count #type: ignore

        memo[id(self)] = obj
        return obj #type: ignore

    #---------------------------------------------------------------------------
    def _extract_labels(self,
            mapping: tp.Optional[tp.Dict[tp.Hashable, int]],
            labels: np.ndarray,
            dtype: tp.Optional[np.dtype] = None
            ) -> np.ndarray:
        '''Called in Index.__init__(). This creates and populates mutable storage as a side effect of array derivation; this storage will be grown as needed.
        '''
        labels = Index._extract_labels(mapping, labels, dtype)
        self._labels_mutable = labels.tolist()
        if len(labels):
            self._labels_mutable_dtype = labels.dtype
        else: # avoid setting to float default when labels is empty
            self._labels_mutable_dtype = None
        return labels

    def _extract_positions(self,
            size: int,
            positions: tp.Optional[tp.Sequence[int]]
            ) -> np.ndarray:
        '''Called in Index.__init__(). This creates and populates mutable storage. This creates and populates mutable storage as a side effect of array derivation.
        '''
        positions = Index._extract_positions(size, positions)
        self._positions_mutable_count = size
        return positions

    def _update_array_cache(self) -> None:

        if self._labels_mutable_dtype is not None and len(self._labels):
            # only update if _labels_mutable_dtype has been set and _labels exist
            self._labels_mutable_dtype = resolve_dtype(
                    self._labels.dtype,
                    self._labels_mutable_dtype)

        # NOTE: necessary to support creation from iterable of tuples
        self._labels, _ = iterable_to_array_1d(
                self._labels_mutable,
                dtype=self._labels_mutable_dtype)
        self._positions = PositionsAllocator.get(self._positions_mutable_count)
        self._recache = False

    #---------------------------------------------------------------------------
    # grow only mutation

    def append(self, value: tp.Hashable) -> None:
        '''append a value
        '''
        if self.__contains__(value): #type: ignore
            raise KeyError(f'duplicate key append attempted: {value}')

        # we might need to initialize map if not an increment that keeps loc_is_iloc relationship
        initialize_map = False
        if self._map is None: # loc_is_iloc
            if not (isinstance(value, INT_TYPES)
                    and value == self._positions_mutable_count):
                initialize_map = True
        else:
            self._map.add(value)

        if self._labels_mutable_dtype is not None:
            self._labels_mutable_dtype = resolve_dtype(
                    dtype_from_element(value),
                    self._labels_mutable_dtype)
        else:
            self._labels_mutable_dtype = dtype_from_element(value)

        self._labels_mutable.append(value)

        if initialize_map:
            self._map = AutoMap(self._labels_mutable)

        self._positions_mutable_count += 1
        self._recache = True

    def extend(self, values: KeyIterableTypes) -> None:
        '''Append multiple values
        Args:
            values: can be a generator.
        '''
        for value in values:
            self.append(value)


class IndexGO(_IndexGOMixin, Index):
    '''A mapping of labels to positions, immutable with grow-only size. Used as columns in :obj:`FrameGO`.
    '''

    _IMMUTABLE_CONSTRUCTOR = Index
    __slots__ = _INDEX_GO_SLOTS


# update class attr on Index after class initialziation
Index._MUTABLE_CONSTRUCTOR = IndexGO



#-------------------------------------------------------------------------------

def _index_initializer_needs_init(
        value: tp.Optional[IndexInitializer]
        ) -> bool:
    '''Determine if value is a non-empty index initializer. This could almost just be a truthy test, but ndarrays need to be handled in isolation. Generators should return True.
    '''
    if value is None:
        return False
    if isinstance(value, IndexBase):
        return False
    if value.__class__ is np.ndarray:
        return bool(len(value)) #type: ignore
    return bool(value)

