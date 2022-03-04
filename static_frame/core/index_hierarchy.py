import functools
import itertools
import operator
import typing as tp
from ast import literal_eval
from copy import deepcopy

import numpy as np
from arraykit import name_filter

from static_frame.core.container_util import index_from_optional_constructor
from static_frame.core.container_util import matmul
from static_frame.core.container_util import rehierarch_from_type_blocks
from static_frame.core.container_util import key_from_container_key
from static_frame.core.container_util import sort_index_for_order

from static_frame.core.display import Display
from static_frame.core.display import DisplayActive
from static_frame.core.display_config import DisplayConfig
from static_frame.core.display import DisplayHeader
from static_frame.core.doc_str import doc_inject

from static_frame.core.exception import ErrorInitIndex
from static_frame.core.exception import ErrorInitIndexNonUnique
from static_frame.core.hloc import HLoc
from static_frame.core.index import ILoc
from static_frame.core.index import Index
from static_frame.core.index import IndexGO
from static_frame.core.util import iterable_to_array_1d
from static_frame.core.util import PositionsAllocator
from static_frame.core.index import mutable_immutable_index_filter
from static_frame.core.index import immutable_index_filter
from static_frame.core.index_base import IndexBase
from static_frame.core.index_auto import RelabelInput
from static_frame.core.index_datetime import IndexDatetime
from static_frame.core.loc_map import LocMap
from static_frame.core.node_dt import InterfaceDatetime
from static_frame.core.node_iter import IterNodeApplyType
from static_frame.core.node_iter import IterNodeDepthLevel
from static_frame.core.node_iter import IterNodeType
from static_frame.core.node_selector import InterfaceAsType
from static_frame.core.node_selector import InterfaceGetItem
from static_frame.core.node_selector import TContainer
from static_frame.core.node_str import InterfaceString
from static_frame.core.node_transpose import InterfaceTranspose
from static_frame.core.node_re import InterfaceRe
from static_frame.core.type_blocks import TypeBlocks

from static_frame.core.util import DEFAULT_SORT_KIND
from static_frame.core.util import DepthLevelSpecifier
from static_frame.core.util import DtypeSpecifier
from static_frame.core.util import EMPTY_TUPLE
from static_frame.core.util import DTYPE_BOOL
from static_frame.core.util import DTYPE_INT_DEFAULT
from static_frame.core.util import DTYPE_OBJECT
from static_frame.core.util import KEY_MULTIPLE_TYPES
from static_frame.core.util import GetItemKeyType
from static_frame.core.util import IndexConstructor
from static_frame.core.util import IndexConstructors
from static_frame.core.util import IndexInitializer
from static_frame.core.util import INT_TYPES
from static_frame.core.util import intersect2d
from static_frame.core.util import isin
from static_frame.core.util import NAME_DEFAULT
from static_frame.core.util import NameType
from static_frame.core.util import NULL_SLICE
from static_frame.core.util import setdiff2d
from static_frame.core.util import UFunc
from static_frame.core.util import union2d
from static_frame.core.util import array2d_to_array1d
from static_frame.core.util import iterable_to_array_2d
from static_frame.core.util import array_sample
from static_frame.core.util import arrays_equal
from static_frame.core.util import array_to_duplicated
from static_frame.core.util import key_to_datetime_key
from static_frame.core.util import CONTINUATION_TOKEN_INACTIVE
from static_frame.core.util import BoolOrBools
from static_frame.core.util import isna_array
from static_frame.core.util import isin_array
from static_frame.core.util import ufunc_unique
from static_frame.core.util import ufunc_unique1d_counts
from static_frame.core.util import ufunc_unique1d_indexer
from static_frame.core.util import ufunc_unique1d_positions
from static_frame.core.util import ufunc_unique2d
from static_frame.core.util import view_2d_as_1d

from static_frame.core.style_config import StyleConfig

if tp.TYPE_CHECKING:
    from pandas import DataFrame #pylint: disable=W0611 # pragma: no cover
    from static_frame.core.frame import Frame #pylint: disable=W0611,C0412 # pragma: no cover
    from static_frame.core.frame import FrameGO #pylint: disable=W0611,C0412 # pragma: no cover
    from static_frame.core.series import Series #pylint: disable=W0611,C0412 # pragma: no cover

IH = tp.TypeVar('IH', bound='IndexHierarchy')
IHGO = tp.TypeVar('IHGO', bound='IndexHierarchyGO')
IHAsType = tp.TypeVar('IHAsType', bound='IndexHierarchyAsType')

SingleLabelType = tp.Tuple[tp.Hashable, ...]
TreeNodeT = tp.Dict[tp.Hashable, tp.Union[tp.Sequence[tp.Hashable], 'TreeNodeT']]

_NBYTES_GETTER = operator.attrgetter('nbytes')


def build_indexers_from_product(list_lengths: tp.Sequence[int]) -> tp.List[np.ndarray]:
    '''
    Creates a list of indexer arrays given a sequence of `list_lengths`

    This is equivalent to: ``np.array(list(itertools.product(*(range(l) for l in list_lengths))))``
    except it scales incredibly well.

    It observes that the indexers for a product will look like this:

    Example:

    lengths: (3, 3, 3)
    result:
        [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2] # 0
        [0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1] # 1
        [0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2] # 2

    We can think of each depth level as repeating two parts: elements & groups.

    For depth 0, each element is repeated 6x, each group 1x
    For depth 1, each element is repeated 3x, each group 3x
    For depth 2, each element is repeated 1x, each group 6x

    We can take advantage of this clear pattern by using cumulative sums to
    determine what those repetitions are, and then apply them.
    '''

    padded_lengths = np.full(len(list_lengths) + 2, 1, dtype=DTYPE_INT_DEFAULT)
    padded_lengths[1:-1] = list_lengths

    all_group_reps = np.cumprod(padded_lengths)[:-2]
    all_index_reps = np.cumprod(padded_lengths[::-1])[-3::-1]

    result = []

    for i, (group_reps, index_reps) in enumerate(zip(all_group_reps, all_index_reps)):
        group = np.hstack(
            tuple(
                map(
                    # Repeat each index (i.e. element) `index_reps` times
                    functools.partial(np.tile, reps=index_reps),
                    range(padded_lengths[i+1]),
                )
            )
        )

        # Repeat each group `group_reps` times
        indexer = np.tile(group, reps=group_reps)
        indexer.flags.writeable = False
        result.append(indexer)

    return result


def blocks_to_container(blocks: tp.Iterator[np.ndarray]) -> np.ndarray:
    return TypeBlocks.from_blocks(blocks).values


def construct_indices_and_indexers_from_column_arrays(
        *,
        column_iter: tp.Iterable[np.ndarray],
        index_constructors_iter: tp.Iterable[IndexConstructor],
        ) -> tp.Tuple[tp.List[Index], tp.List[np.ndarray]]:
    indices: tp.List[Index] = []
    indexers: tp.List[np.ndarray] = []

    for column, constructor in zip(column_iter, index_constructors_iter):
        # Alternative approach that retains order
        # positions, indexer = ufunc_unique1d_positions(column)
        # unsorted_unique = column[np.sort(positions)]
        # indexer_remap = ufunc_unique1d_indexer(unsorted_unique)[1]
        # indexer = indexer_remap[indexer]
        # unique_values = unsorted_unique

        unique_values, indexer = ufunc_unique1d_indexer(column)

        # we call the constructor on all lvl, even if it is already an Index
        indices.append(constructor(unique_values))
        indexers.append(indexer)

    return indices, indexers


class PendingRow:
    '''
    Encapsulates a new label row that has yet to be inserted into a IndexHierarchy.
    '''
    def __init__(self, row: SingleLabelType) -> None:
        self.row = row

    def __len__(self) -> int:
        '''Each row is a single label in an IndexHierarchy'''
        return 1

    def __iter__(self) -> tp.Iterator[tp.Hashable]:
        yield from self.row


# ------------------------------------------------------------------------------

class IndexHierarchy(IndexBase):
    '''
    A hierarchy of :obj:`Index` objects.
    '''

    __slots__ = (
            '_indices',
            '_indexers',
            '_name',
            '_blocks',
            '_recache',
            '_index_types',
            '_pending_extensions',
            )

    _indices: tp.List[Index] # Of index objects
    _indexers: tp.List[np.ndarray] # integer arrays
    _name: NameType
    _blocks: TypeBlocks
    _recache: bool
    _index_types: tp.Optional['Series'] # Used to cache the property `index_types`
    _pending_extensions: tp.Optional[tp.List[tp.Union[SingleLabelType, 'IndexHierarchy']]]

    # _IMMUTABLE_CONSTRUCTOR is None from IndexBase
    # _MUTABLE_CONSTRUCTOR will be defined after IndexHierarhcyGO defined

    _INDEX_CONSTRUCTOR: IndexConstructor = Index

    _UFUNC_UNION = union2d
    _UFUNC_INTERSECTION = intersect2d
    _UFUNC_DIFFERENCE = setdiff2d
    _NDIM: int = 2

    # --------------------------------------------------------------------------
    # constructors

    @classmethod
    def _build_index_constructors(cls: tp.Type[IH],
            *,
            index_constructors: IndexConstructors,
            depth: int,
            ) -> tp.Iterable[IndexConstructor]:
        '''
        Returns an iterable of `depth` number of index constructors based on user-provided `index_constructors`
        '''
        if index_constructors is None:
            return (cls._INDEX_CONSTRUCTOR for _ in range(depth))

        if callable(index_constructors): # support a single constrctor
            return (index_constructors for _ in range(depth))

        index_constructors = tuple(index_constructors)

        if len(index_constructors) != depth:
            raise ErrorInitIndex(
                'When providing multiple index constructors, their number must equal the depth of the IndexHierarchy.'
            )

        return index_constructors

    @classmethod
    def _build_name_from_indices(cls: tp.Type[IH],
            indices: tp.List[Index],
            ) -> tp.Optional[SingleLabelType]:
        '''
        Builds the IndexHierarchy name from the names of `indices`. If one is not specified, the name is None
        '''
        name: SingleLabelType = tuple(index.name for index in indices)
        if any(n is None for n in name):
            return None

        return name

    @classmethod
    def from_product(cls: tp.Type[IH],
            *levels: IndexInitializer,
            name: NameType = None,
            index_constructors: IndexConstructors = None,
            ) -> IH:
        '''
        Given groups of iterables, return an ``IndexHierarchy`` made of the product of a values in those groups, where the first group is the top-most hierarchy.

        Args:
            *levels: index initializers (or Index instances) for each level
            name:
            index_consructors:

        Returns:
            :obj:`static_frame.IndexHierarchy`

        '''
        if len(levels) == 1:
            raise ErrorInitIndex('Cannot create IndexHierarchy from only one level.')

        indices = [] # store in a list, where index is depth

        index_constructors_iter = cls._build_index_constructors(
                index_constructors=index_constructors,
                depth=len(levels),
                )

        for lvl, constructor in zip(levels, index_constructors_iter):
            if isinstance(lvl, Index):
                # TODO: This is slighly different behavior from IndexHierarchyTree, as it called `constructor` for both cases
                indices.append(immutable_index_filter(lvl))
            else:
                indices.append(constructor(lvl))

        if name is None:
            name = cls._build_name_from_indices(indices)

        indexers = build_indexers_from_product(tuple(map(len, indices)))

        return cls(
                name=name,
                indices=indices,
                indexers=indexers,
                )

    @classmethod
    def _from_tree(cls: tp.Type[IH],
            tree: TreeNodeT,
            ) -> tp.Iterator[SingleLabelType]:
        '''
        Yields all the labels provided by a `tree`
        '''
        for label, subtree in tree.items():
            if isinstance(subtree, dict):
                for treerow in cls._from_tree(subtree):
                    yield (label, *treerow)
            else:
                for row in subtree:
                    yield (label, row)

    @classmethod
    def from_tree(cls: tp.Type[IH],
            tree: TreeNodeT,
            *,
            name: NameType = None,
            index_constructors: IndexConstructors = None,
            ) -> IH:
        '''
        Convert into a ``IndexHierarchy`` a dictionary defining keys to either iterables or nested dictionaries of the same.

        Returns:
            :obj:`static_frame.IndexHierarchy`
        '''
        return cls.from_labels(
                labels=cls._from_tree(tree),
                name=name,
                index_constructors=index_constructors,
                )

    @classmethod
    def _from_empty(cls: tp.Type[IH],
            empty_labels: tp.Iterable[tp.Sequence[tp.Hashable]],
            *,
            name: NameType = None,
            depth_reference: tp.Optional[int] = None,
            ) -> IH:
        '''
        Construct an IndexHierarchy from an iterable of empty labels.
        '''
        if empty_labels.__class__ is np.ndarray and empty_labels.ndim == 2: # type: ignore
            # if this is a 2D array, we can get the depth
            depth = empty_labels.shape[1] # type: ignore

            if depth == 0: # an empty 2D array can have 0 depth
                pass # do not set depth_reference, assume it is set
            elif (depth_reference is None and depth > 1) or (
                depth_reference is not None and depth_reference == depth
            ):
                depth_reference = depth
            else:
                raise ErrorInitIndex(
                    f'depth_reference provided {depth_reference} does not match depth of supplied array {depth}'
                )

        if not isinstance(depth_reference, INT_TYPES):
            raise ErrorInitIndex(
                'depth_reference must be an integer when labels are empty.'
            )

        if depth_reference == 1:
            raise ErrorInitIndex('Cannot create IndexHierarchy from only one level.')

        return cls(
                indices=[
                    cls._INDEX_CONSTRUCTOR(EMPTY_TUPLE) for _ in range(depth_reference)
                ],
                indexers=[PositionsAllocator.get(0) for _ in range(depth_reference)],
                name=name
                )

    @classmethod
    def _from_array(cls: tp.Type[IH],
            array: np.ndarray,
            *,
            name: NameType = None,
            depth_reference: tp.Optional[DepthLevelSpecifier] = None,
            index_constructors: IndexConstructors = None,
            ) -> IH:
        '''
        Construct an :obj:`IndexHierarchy` from a 2D numpy array

        Very similar implementation to :meth:`_from_type_blocks`

        Returns:
            :obj:`IndexHierarchy`
        '''
        try:
            _, depth = array.shape
        except ValueError:
            raise ErrorInitIndex('Array must be 2-dimensional.')

        if not array.size:
            return cls._from_empty(array, name=name, depth_reference=depth_reference)

        index_constructors_iter = cls._build_index_constructors(
                index_constructors=index_constructors,
                depth=depth,
                )

        indices, indexers = construct_indices_and_indexers_from_column_arrays(
                column_iter=array.T,
                index_constructors_iter=index_constructors_iter,
                )

        return cls(indices=indices, indexers=indexers, name=name)

    @classmethod
    def from_labels(cls: tp.Type[IH],
            labels: tp.Iterable[tp.Sequence[tp.Hashable]],
            *,
            name: NameType = None,
            reorder_for_hierarchy: bool = False,
            index_constructors: IndexConstructors = None,
            depth_reference: tp.Optional[DepthLevelSpecifier] = None,
            continuation_token: tp.Union[tp.Hashable, None] = CONTINUATION_TOKEN_INACTIVE
            ) -> IH:
        '''
        Construct an ``IndexHierarchy`` from an iterable of labels, where each label is tuple defining the component labels for all hierarchies.

        Args:
            labels: an iterator or generator of tuples.
            *,
            name:
            reorder_for_hierarchy: an optional argument that will ensure the resulting index is arranged in a tree-like structure.
            index_constructors:
            depth_reference:
            continuation_token: a Hashable that will be used as a token to identify when a value in a label should use the previously encountered value at the same depth.

        Returns:
            :obj:`static_frame.IndexHierarchy`
        '''
        labels_iter = iter(labels)

        try:
            label_row = next(labels_iter)
        except StopIteration:
            labels_are_empty = True
        else:
            labels_are_empty = False

        if labels_are_empty:
            return cls._from_empty(labels, name=name, depth_reference=depth_reference)

        depth = len(label_row)

        if depth == 1:
            raise ErrorInitIndex('Cannot create IndexHierarchy from only one level.')

        # A mapping for each depth level, of label to index
        hash_maps: tp.List[tp.Dict[tp.Hashable, int]] = [{} for _ in range(depth)]
        indexers: tp.List[tp.List[int]] = [[] for _ in range(depth)]

        prev_row: tp.Sequence[tp.Hashable] = EMPTY_TUPLE

        while True:
            for hash_map, indexer, val in zip(hash_maps, indexers, label_row):

                # The equality check is heavy, so we short circuit when possible on an `is` check
                if (
                    continuation_token is not CONTINUATION_TOKEN_INACTIVE
                    and val == continuation_token
                ):
                    if prev_row:
                        i = indexer[-1] # Repeat the last observed index
                    else:
                        # This is the first row!
                        i = 0
                        hash_map[val] = 0
                elif val not in hash_map:
                    i = len(hash_map)
                    hash_map[val] = i
                else:
                    i = hash_map[val]

                indexer.append(i)

            prev_row = label_row
            try:
                label_row = next(labels_iter)
            except StopIteration:
                break

            if len(label_row) != depth:
                raise ErrorInitIndex('All labels must have the same depth.')

        # Convert to numpy arrays
        for i in range(depth):
            indexers[i] = np.array(indexers[i], dtype=DTYPE_INT_DEFAULT)

        if reorder_for_hierarchy:
            # The innermost level (i.e. [:-1]) is irrelavant to lexsorting
            # We sort lexsort from right to left (i.e. [::-1])
            sort_order = np.lexsort(indexers[:-1][::-1])
            indexers = [indexer[sort_order] for indexer in indexers]

        for i in range(depth):
            indexers[i].flags.writeable = False # type: ignore

        index_constructors_iter = cls._build_index_constructors(
                index_constructors=index_constructors,
                depth=depth,
                )

        indices = [
            constructor(hash_map)
            for constructor, hash_map in zip(index_constructors_iter, hash_maps)
        ]

        if name is None:
            name = cls._build_name_from_indices(indices)

        return cls(indices=indices, indexers=indexers, name=name)

    @classmethod
    def from_index_items(cls: tp.Type[IH],
            items: tp.Iterable[tp.Tuple[tp.Hashable, Index]],
            *,
            index_constructor: tp.Optional[IndexConstructor] = None,
            name: NameType = None,
            ) -> IH:
        '''
        Given an iterable of pairs of label, :obj:`Index`, produce an :obj:`IndexHierarchy` where the labels are depth 0, the indices are depth 1.

        Args:
            items: iterable of pairs of label, :obj:`Index`.
            index_constructor: Optionally provide index constructor for outermost index.
        '''
        labels: tp.List[tp.Hashable] = []
        index_inner: tp.Optional[IndexGO] = None  # We will grow this in-place
        indexers_inner: tp.List[np.ndarray] = []

        # Contains the len of the index for each label. Used to generate the outermost indexer
        repeats: tp.List[int] = []

        for label, index in items:
            labels.append(label)

            if index_inner is None:
                # This is the first index. Convert to IndexGO
                index_inner = tp.cast(IndexGO, mutable_immutable_index_filter(False, index))
                new_indexer = PositionsAllocator.get(len(index_inner))
            else:
                new_labels = index.difference(index_inner) # Retains order!

                if new_labels.size:
                    index_inner.extend(new_labels.values)

                new_indexer = index._index_iloc_map(index_inner)

            indexers_inner.append(new_indexer)
            repeats.append(len(index))

        if not labels:
            assert index_inner is None # sanity check
            return cls._from_empty(EMPTY_TUPLE, name=name, depth_reference=2)

        index_inner = mutable_immutable_index_filter(cls.STATIC, index_inner) # type: ignore

        def _repeat(i_repeat_tuple: tp.Tuple[int, int]) -> np.ndarray:
            i, repeat = i_repeat_tuple
            return np.tile(i, reps=repeat)

        indexers: tp.List[np.ndarray] = []
        indexers.append(np.hstack(tuple(map(_repeat, enumerate(repeats))))) # outermost level
        indexers.append(np.hstack(indexers_inner)) # innermost level

        indexers[0].flags.writeable = False
        indexers[1].flags.writeable = False

        index_outer = index_from_optional_constructor(
                labels,
                default_constructor=cls._INDEX_CONSTRUCTOR,
                explicit_constructor=index_constructor,
                )

        return cls(
                indices=[index_outer, index_inner], # type: ignore
                indexers=indexers,
                name=name,
                )

    @classmethod
    def from_labels_delimited(cls: tp.Type[IH],
            labels: tp.Iterable[str],
            *,
            delimiter: str = ' ',
            name: NameType = None,
            index_constructors: IndexConstructors = None,
            ) -> IH:
        '''
        Construct an :obj:`IndexHierarchy` from an iterable of labels, where each label is string defining the component labels for all hierarchies using a string delimiter. All components after splitting the string by the delimited will be literal evaled to produce proper types; thus, strings must be quoted.

        Args:
            labels: an iterator or generator of tuples.

        Returns:
            :obj:`static_frame.IndexHierarchy`
        '''
        def to_label(label: str) -> SingleLabelType:

            start, stop = None, None
            if label[0] in ('[', '('):
                start = 1
            if label[-1] in (']', ')'):
                stop = -1

            if start is not None or stop is not None:
                label = label[start: stop]

            parts = label.split(delimiter)
            if len(parts) <= 1:
                raise RuntimeError(
                    f'Could not not parse more than one label from delimited string: {label}'
                )

            try:
                return tuple(literal_eval(p) for p in parts)
            except ValueError as e:
                raise ValueError(
                    'A label is malformed. This may be due to not quoting a string label'
                ) from e

        return cls.from_labels(
                (to_label(label) for label in labels),
                name=name,
                index_constructors=index_constructors
                )

    @classmethod
    def from_names(cls: tp.Type[IH],
            names: tp.Iterable[tp.Hashable],
            ) -> IH:
        '''
        Construct a zero-length :obj:`IndexHierarchy` from an iterable of ``names``, where the length of ``names`` defines the zero-length depth.

        Args:
            names: Iterable of hashable names per depth.
        '''
        name = tuple(names)
        if len(name) == 0:
            raise ErrorInitIndex('names must be non-empty.')

        return cls._from_empty(EMPTY_TUPLE, name=name, depth_reference=len(name))

    @classmethod
    def _from_type_blocks(cls: tp.Type[IH],
            blocks: TypeBlocks,
            *,
            name: NameType = None,
            index_constructors: IndexConstructors = None,
            own_blocks: bool = False,
            ) -> IH:
        '''
        Construct an :obj:`IndexHierarchy` from a :obj:`TypeBlocks` instance.

        Args:
            blocks: a TypeBlocks instance

        Returns:
            :obj:`IndexHierarchy`
        '''
        size, depth = blocks.shape
        if depth == 1:
            raise ErrorInitIndex('blocks must have at least two dimensions.')

        def gen_columns() -> tp.Iterator[np.ndarray]:
            for i in range(blocks.shape[1]):
                block = blocks._extract(column_key=i)
                yield block.values.reshape(size)

        index_constructors_iter = cls._build_index_constructors(
                index_constructors=index_constructors,
                depth=blocks.shape[1],
                )

        indices, indexers = construct_indices_and_indexers_from_column_arrays(
                column_iter=gen_columns(),
                index_constructors_iter=index_constructors_iter,
                )

        # Use a new variable name to make mypy happy
        init_blocks: tp.Optional[TypeBlocks] = blocks

        if index_constructors is not None:
            # If defined, we may have changed columnar dtypes in IndexLevels, and cannot reuse blocks
            if tuple(blocks.dtypes) != tuple(index.dtype for index in indices):
                init_blocks = None
                own_blocks = False

        return cls(
                indices=indices,
                indexers=indexers,
                name=name,
                blocks=init_blocks,
                own_blocks=own_blocks,
                )

    @staticmethod
    def _ensure_uniqueness(
            indexers: tp.List[np.ndarray],
            values: np.ndarray,
            ) -> None:
        '''
        Determines whether or not `indexers` are unique
        '''
        expected_len = len(values)

        if expected_len <= 1:
            return # Not possible to be duplicated!

        unique_len = len(ufunc_unique2d(np.array(indexers).T, axis=0))

        if unique_len != expected_len:
            # TODO: Is this too much work to do for an error message?
            duplicates = array_to_duplicated(
                    np.array(indexers),
                    axis=1,
                    exclude_first=True,
                    exclude_last=False,
                    )
            first_duplicate = values[np.argmax(duplicates)]
            # TODO: Should this show more examples than just the first observation?
            msg = f'Labels have {sum(duplicates)} non-unique values, including {tuple(first_duplicate)}.'
            raise ErrorInitIndexNonUnique(msg)

    def _create_blocks_from_self(self: IH) -> TypeBlocks:
        '''
        Create a :obj:`TypeBlocks` instance from values respresented by `self._indices` and `self._indexers`.
        '''
        def gen_blocks() -> tp.Iterator[np.ndarray]:
            for i, index in enumerate(self._indices):
                indexer = self._indexers[i]
                yield index.values[indexer]

        return TypeBlocks.from_blocks(gen_blocks())

    # --------------------------------------------------------------------------

    def __init__(self: IH,
            indices: tp.Union[IH, tp.List[Index]],
            *,
            indexers: tp.List[np.ndarray] = EMPTY_TUPLE, # type: ignore
            name: NameType = NAME_DEFAULT,
            blocks: tp.Optional[TypeBlocks] = None,
            own_blocks: bool = False,
            ) -> None:
        '''
        Initializer.

        Args:
            indices: list of :obj:`Index` objects
            indexers: list of indexer arrays
            name: name of the IndexHierarchy
            blocks:
            own_blocks:
        '''
        self._recache = False
        self._index_types = None
        self._pending_extensions = None

        if isinstance(indices, IndexHierarchy):
            if indexers:
                raise ErrorInitIndex(
                    'indexers must not be provided when copying an IndexHierarchy'
                )
            if blocks is not None:
                raise ErrorInitIndex(
                    'blocks must not be provided when copying an IndexHierarchy'
                )

            if indices._recache:
                indices._update_array_cache()

            self._indices = [
                mutable_immutable_index_filter(self.STATIC, index)
                for index in indices._indices
            ]
            self._indexers = list(indices._indexers)
            self._name = name if name is not None else indices._name
            self._blocks = indices._blocks
            return

        if not all(arr.__class__ is np.ndarray for arr in indexers):
            raise ErrorInitIndex('indexers must be numpy arrays.')

        if not all(not arr.flags.writeable for arr in indexers):
            raise ErrorInitIndex('indexers must be read-only.')

        if not all(isinstance(index, Index) for index in indices):
            raise ErrorInitIndex("indices must all Index's!")

        if len(indices) <= 1:
            raise ErrorInitIndex('Index Hierarchies must have at least two levels!')

        self._indices = [
            mutable_immutable_index_filter(self.STATIC, index) # type: ignore
            for index in indices
        ]
        self._indexers = indexers
        self._name = None if name is NAME_DEFAULT else name_filter(name)

        if blocks is not None:
            if own_blocks:
                self._blocks = blocks
            else:
                self._blocks = blocks.copy()
        else:
            self._blocks = self._create_blocks_from_self()

        self._ensure_uniqueness(self._indexers, self.values)

    def _append(self: IH,
            row: SingleLabelType,
            indexers: tp.List[np.ndarray],
            offset: int,
            ) -> None:
        raise NotImplementedError('Must be implemented on IndexHierarchyGO only') # pragma: no cover

    def _extend(self: IH,
            other: IH,
            indexers: tp.List[np.ndarray],
            offset: int,
            size: int,
            ) -> None:
        raise NotImplementedError('Must be implemented on IndexHierarchyGO only') # pragma: no cover

    def _update_array_cache(self: IH) -> None:
        new_indexers = [np.empty(self.__len__(), DTYPE_INT_DEFAULT) for _ in range(self.depth)]

        current_size = len(self._blocks)

        for depth, indexer in enumerate(self._indexers):
            new_indexers[depth][:current_size] = indexer

        self._indexers.clear()
        # TODO: What if an error is raised anywhere onward in this method?

        offset = current_size

        for pending in self._pending_extensions: # type: ignore
            if isinstance(pending, PendingRow):
                self._append(pending, new_indexers, offset)
                offset += 1
            else:
                group_size = len(pending)
                self._extend(pending, new_indexers, offset, group_size)
                offset += group_size

        self._pending_extensions.clear() # type: ignore

        for indexer in new_indexers:
            indexer.flags.writeable = False

        self._indexers = new_indexers

        self._blocks = self._create_blocks_from_self()
        self._ensure_uniqueness(self._indexers, self.values)

        self._recache = False

    # --------------------------------------------------------------------------

    def __deepcopy__(self: IH,
            memo: tp.Dict[int, tp.Any],
            ) -> IH:
        '''
        Return a deep copy of this IndexHierarchy.
        '''
        # TODO: Should self recache, or do an exact copy at this current point in time.
        obj: IH = self.__new__(self.__class__)
        obj._indices = deepcopy(self._indices, memo)
        obj._indexers = deepcopy(self._indexers, memo)
        obj._blocks = deepcopy(self._blocks, memo)
        obj._name = self._name # should be hashable/immutable
        obj._recache = self._recache
        obj._index_types = deepcopy(self._index_types, memo)
        obj._pending_extensions = deepcopy(self._pending_extensions, memo)

        memo[id(self)] = obj
        return obj

    def __copy__(self: IH) -> IH:
        '''
        Return a shallow copy of this IndexHierarchy.
        '''
        # We don't recache here since IndexHierarchyGO overrides __copy__
        blocks = self._blocks.copy()
        return self.__class__(
                indices=list(self._indices),
                indexers=list(self._indexers),
                name=self._name,
                blocks=blocks,
                own_blocks=True,
                )

    def copy(self: IH) -> IH:
        '''
        Return a shallow copy of this IndexHierarchy.
        '''
        return self.__copy__()

    # --------------------------------------------------------------------------
    # name interface

    def rename(self: IH,
            name: NameType,
            ) -> IH:
        '''
        Return a new IndexHierarchy with an updated name attribute.
        '''
        return self.__class__(self, name=name)

    # --------------------------------------------------------------------------
    # interfaces

    @property
    def loc(self: IH) -> InterfaceGetItem['IndexHierarchy']:
        return InterfaceGetItem(self._extract_loc)

    @property
    def iloc(self: IH) -> InterfaceGetItem['IndexHierarchy']:
        return InterfaceGetItem(self._extract_iloc)

    def _iter_label(self: IH,
            depth_level: tp.Optional[DepthLevelSpecifier] = None,
            ) -> tp.Iterator[tp.Hashable]:
        '''
        Iterate over labels at a given depth level.

        For multiple depth levels, the iterator will yield tuples of labels.
        '''
        if depth_level is None: # default to full labels
            depth_level = list(range(self.depth))

        if isinstance(depth_level, INT_TYPES):
            yield from self.values_at_depth(depth_level)
        else:
            yield from zip(*map(self.values_at_depth, depth_level))

    def _iter_label_items(self: IH,
            depth_level: tp.Optional[DepthLevelSpecifier] = None,
            ) -> tp.Iterator[tp.Tuple[int, tp.Hashable]]:
        '''
        This function is not directly called in iter_label or related routines, fulfills the expectations of the IterNodeDepthLevel interface.
        '''
        yield from enumerate(self._iter_label(depth_level=depth_level))

    @property
    def iter_label(self: IH) -> IterNodeDepthLevel[tp.Any]:
        return IterNodeDepthLevel(
                container=self,
                function_items=self._iter_label_items,
                function_values=self._iter_label,
                yield_type=IterNodeType.VALUES,
                apply_type=IterNodeApplyType.INDEX_LABELS
                )

    # NOTE: Index implements drop property

    @property
    @doc_inject(select='astype')
    def astype(self: IH) -> InterfaceAsType[TContainer]:
        '''
        Retype one or more depths. Can be used as as function to retype the entire ``IndexHierarchy``; alternatively, a ``__getitem__`` interface permits retyping selected depths.

        Args:
            {dtype}
        '''
        return InterfaceAsType(func_getitem=self._extract_getitem_astype)

    # --------------------------------------------------------------------------

    @property
    def via_str(self: IH) -> InterfaceString[np.ndarray]:
        '''
        Interface for applying string methods to elements in this container.
        '''
        if self._recache:
            self._update_array_cache()

        return InterfaceString(
                blocks=self._blocks._blocks,
                blocks_to_container=blocks_to_container,
                )

    @property
    def via_dt(self: IH) -> InterfaceDatetime[np.ndarray]:
        '''
        Interface for applying datetime properties and methods to elements in this container.
        '''
        if self._recache:
            self._update_array_cache()

        return InterfaceDatetime(
                blocks=self._blocks._blocks,
                blocks_to_container=blocks_to_container,
                )

    @property
    def via_T(self: IH) -> InterfaceTranspose['IndexHierarchy']:
        '''
        Interface for using binary operators with one-dimensional sequences, where the opperand is applied column-wise.
        '''
        return InterfaceTranspose(container=self)

    def via_re(self: IH,
            pattern: str,
            flags: int = 0,
            ) -> InterfaceRe[np.ndarray]:
        '''
        Interface for applying regular expressions to elements in this container.
        '''
        if self._recache:
            self._update_array_cache()

        return InterfaceRe(
                blocks=self._blocks._blocks,
                blocks_to_container=blocks_to_container,
                pattern=pattern,
                flags=flags,
                )

    # --------------------------------------------------------------------------

    @property
    @doc_inject()
    def mloc(self: IH) -> np.ndarray:
        '''
        {doc_int}
        '''
        if self._recache:
            self._update_array_cache()

        return self._blocks.mloc

    @property
    def dtypes(self: IH) -> 'Series':
        '''
        Return a Series of dytpes for each index depth.

        Returns:
            :obj:`static_frame.Series`
        '''
        from static_frame.core.series import Series

        if (
            self._name
            and isinstance(self._name, tuple)
            and len(self._name) == self.depth
        ):
            labels: NameType = self._name
        else:
            labels = None

        return Series((index.dtype for index in self._indices), index=labels)

    @property
    def shape(self: IH) -> tp.Tuple[int, ...]:
        '''
        Return a tuple describing the shape of the underlying NumPy array.

        Returns:
            :obj:`tp.Tuple[int]`
        '''
        if self._recache:
            return self.__len__(), self.depth

        return self._blocks._shape

    @property
    def ndim(self: IH) -> int:
        '''
        Return the number of dimensions.

        Returns:
            :obj:`int`
        '''
        return self._NDIM

    @property
    def size(self: IH) -> int:
        '''
        Return the size of the underlying NumPy array.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()

        return self._blocks.size

    @property
    def nbytes(self: IH) -> int:
        '''
        Return the total bytes of the underlying NumPy array.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()

        total = sum(map(_NBYTES_GETTER, self._indices))
        total += sum(map(_NBYTES_GETTER, self._indexers))
        total += self._blocks.nbytes
        return self._blocks.nbytes

    # --------------------------------------------------------------------------

    def __len__(self: IH) -> int:
        if self._recache:
            size = self._blocks.__len__()

            if self._pending_extensions:
                size += sum(map(len, self._pending_extensions))

            return size

        return self._blocks.__len__()

    @doc_inject()
    def display(self: IH,
            config: tp.Optional[DisplayConfig] = None,
            *,
            style_config: tp.Optional[StyleConfig] = None,
            ) -> Display:
        '''
        {doc}

        Args:
            {config}
        '''
        if self._recache:
            self._update_array_cache()

        config = config or DisplayActive.get()

        sub_display: tp.Optional[Display] = None

        header_sub: tp.Optional[str]
        header: tp.Optional[DisplayHeader]

        if config.type_show:
            header = DisplayHeader(self.__class__, self._name)
            header_depth = 1
            header_sub = '' # need spacer
        else:
            header = None
            header_depth = 0
            header_sub = None

        for col in self._blocks.axis_values(0):
            # as a slice this is far more efficient as no copy is made
            if sub_display is None: # the first
                sub_display = Display.from_values(
                        col,
                        header=header,
                        config=config,
                        outermost=True,
                        index_depth=0,
                        header_depth=header_depth,
                        style_config=style_config,
                        )
            else:
                sub_display.extend_iterable(col, header=header_sub)

        return sub_display # type: ignore

    # --------------------------------------------------------------------------
    # set operations

    def _ufunc_set(self: IH,
            func: tp.Callable[[np.ndarray, np.ndarray, bool], np.ndarray],
            other: tp.Union['IndexBase', tp.Iterable[tp.Hashable]]
            ) -> IH:
        '''
        Utility function for preparing and collecting values for Indices to produce a new Index.
        '''
        if self.equals(other, compare_dtype=True):
            # compare dtype as result should be resolved, even if values are the same
            if (
                func is self.__class__._UFUNC_INTERSECTION
                or func is self.__class__._UFUNC_UNION
            ):
                # NOTE: this will delegate name attr
                return self if self.STATIC else self.copy()
            elif func is self.__class__._UFUNC_DIFFERENCE:
                # we will no longer have type associations per depth
                return self._from_empty(EMPTY_TUPLE, depth_reference=self.depth)
            else:
                raise NotImplementedError(f'Unsupported ufunc {func}') # pragma: no cover

        if self._recache:
            self._update_array_cache()

        if isinstance(other, np.ndarray):
            operand = other
            assume_unique = False
        elif isinstance(other, IndexBase):
            operand = other.values
            assume_unique = True # can always assume unique
        else:
            operand = iterable_to_array_2d(other)
            assume_unique = False

        both_sized = len(operand) > 0 and self.__len__() > 0

        if operand.ndim != 2:
            raise ErrorInitIndex(
                'operand in IndexHierarchy set operations must ndim of 2'
            )
        if both_sized and self.shape[1] != operand.shape[1]:
            raise ErrorInitIndex(
                'operands in IndexHierarchy set operations must have matching depth'
            )

        cls = self.__class__

        # using assume_unique will permit retaining order when operands are identical
        labels = func(self.values, operand, assume_unique=assume_unique) # type: ignore

        # derive index_constructors for IndexHierarchy
        index_constructors: tp.Optional[tp.Sequence[tp.Type[IndexBase]]]

        if both_sized and isinstance(other, IndexHierarchy):
            index_constructors = []
            # depth, and length of index_types, must be equal
            for cls_self, cls_other in zip(
                    self._index_constructors,
                    other._index_constructors,
                    ):
                if cls_self == cls_other:
                    index_constructors.append(cls_self)
                else:
                    index_constructors.append(Index)
        else:
            # if other is not an IndexHierarchy, do not try to propagate types
            index_constructors = None

        return cls._from_array(
                labels,
                name=self.name,
                index_constructors=index_constructors,
                depth_reference=self.depth,
                )

    # --------------------------------------------------------------------------

    @property
    def _index_constructors(self: IH) -> tp.Iterator[tp.Type[Index]]:
        '''
        Yields the index constructors for each depth.
        '''
        yield from (index.__class__ for index in self._indices)

    def _drop_iloc(self: IH,
            key: GetItemKeyType,
            ) -> IH:
        '''
        Create a new index after removing the values specified by the iloc key.
        '''
        if self._recache:
            self._update_array_cache()

        blocks = TypeBlocks.from_blocks(self._blocks._drop_blocks(row_key=key))

        # We could potentially re-use the same indices. Would that be worth it?

        return self.__class__._from_type_blocks(
                blocks=blocks,
                index_constructors=self._index_constructors,
                name=self._name,
                own_blocks=True,
                )

    def _drop_loc(self: IH,
            key: GetItemKeyType,
            ) -> IH:
        '''
        Create a new index after removing the values specified by the loc key.
        '''
        return self._drop_iloc(self._loc_to_iloc(key))

    # --------------------------------------------------------------------------

    @property
    @doc_inject(selector='values_2d', class_name='IndexHierarchy')
    def values(self: IH) -> np.ndarray:
        '''
        {}
        '''
        if self._recache:
            self._update_array_cache()

        return self._blocks.values

    @property
    def positions(self: IH) -> np.ndarray:
        '''
        Return the immutable positions array.
        '''
        return PositionsAllocator.get(self.__len__())

    @property
    def depth(self: IH) -> int: # type: ignore
        '''
        Return the depth of the index hierarchy.
        '''
        return len(self._indices)

    def values_at_depth(self: IH,
            depth_level: DepthLevelSpecifier = 0,
            ) -> np.ndarray:
        '''
        Return an NP array for the ``depth_level`` specified.

        Args:
            depth_level: a single depth level, or iterable depth of depth levels.
        '''
        if self._recache:
            self._update_array_cache()

        sel: GetItemKeyType

        if isinstance(depth_level, INT_TYPES):
            sel = depth_level
        else:
            sel = list(depth_level)

        return self._blocks._extract_array(column_key=sel)

    # NOTE: This is much slower than IndexHierarchyTree. Not sure how to optimize.
    @doc_inject()
    def label_widths_at_depth(self: IH,
            depth_level: DepthLevelSpecifier = 0
            ) -> tp.Iterator[tp.Tuple[tp.Hashable, int]]:
        '''
        {}
        '''
        pos: tp.Optional[int] = None

        if depth_level is None:
            raise NotImplementedError('depth_level of None is not supported')

        if not isinstance(depth_level, INT_TYPES):
            sel = list(depth_level)
            if len(sel) == 1:
                pos = sel.pop()
        else: # is an int
            pos = depth_level

        if pos is None:
            raise NotImplementedError(
                'selecting multiple depth levels is not yet implemented'
            )

        if self._recache:
            self._update_array_cache()

        # Could this be memoized?
        def _extract_counts(
                arr: np.ndarray,
                pos: int,
                ) -> tp.Iterator[tp.Tuple[tp.Hashable, int]]:
            unique, widths = ufunc_unique1d_counts(arr)
            labels = self._indices[pos].values[unique]
            yield from zip(labels, widths)

        # i.e. depth_level is an int
        if pos == 0:
            arr = self._indexers[pos]
            yield from _extract_counts(arr, pos)
            return

        def gen()-> tp.Iterator[tp.Tuple[tp.Hashable, int]]:
            # We need to build up masks for each depth level. This requires the combination
            # of all depths above it. We do not care about order since we are only
            # determining counts. As such, we simply walk from 1 -> len(level) for each outer
            # level
            ranges = tuple(map(range, map(len, self._indices[:pos])))

            for outer_level_idxs in itertools.product(*ranges):
                screen = np.full(self.__len__(), True, dtype=bool)

                for i, outer_level_idx in enumerate(outer_level_idxs):
                    screen &= self._indexers[i] == outer_level_idx

                occurrences: np.ndarray = self._indexers[pos][screen] # type: ignore
                yield from _extract_counts(occurrences, pos)

        yield from gen()

    @property
    def index_types(self: IH) -> 'Series':
        '''
        Return a Series of Index classes for each index depth.

        Returns:
            :obj:`Series`
        '''
        if self._index_types is None:
            # Prefer to use `@functools.cached_property`, but that is not supported
            from static_frame.core.series import Series

            labels: NameType

            if (
                self._name
                and isinstance(self._name, tuple)
                and len(self._name) == self.depth
            ):
                labels = self._name
            else:
                labels = None

            self._index_types = Series(
                    self._index_constructors,
                    index=labels,
                    dtype=DTYPE_OBJECT,
                    )

        return self._index_types

    # --------------------------------------------------------------------------
    def relabel(self: IH,
            mapper: RelabelInput,
            ) -> IH:
        '''
        Return a new IndexHierarchy with labels replaced by the callable or mapping; order will be retained. If a mapping is used, the mapping should map tuple representation of labels, and need not map all origin keys.
        '''
        if self._recache:
            self._update_array_cache()

        if not callable(mapper):
            # if a mapper, it must support both __getitem__ and __contains__
            getitem = getattr(mapper, 'get')

            def gen() -> tp.Iterator[SingleLabelType]:
                for array in self._blocks.axis_values(axis=1):
                    # as np.ndarray are not hashable, must tuplize
                    label = tuple(array)
                    yield getitem(label, label)

            return self.__class__.from_labels(
                    labels=gen(),
                    name=self._name,
                    index_constructors=self._index_constructors,
                    )

        return self.__class__.from_labels(
                (mapper(x) for x in self._blocks.axis_values(axis=1)),
                name=self._name,
                index_constructors=self._index_constructors,
                )

    def relabel_at_depth(self: IH,
            mapper: RelabelInput,
            depth_level: DepthLevelSpecifier = 0,
            ) -> IH:
        '''
        Return a new :obj:`IndexHierarchy` after applying `mapper` to a level or each individual level specified by `depth_level`.

        `mapper` can be a callable, mapping, or iterable.
            - If a callable, it must accept a single value, and return a single value.
            - If a mapping, it must map a single value to a single value.
            - If a iterable, it must be the same length as `self`.

        This call:

        >>> index.relabel_at_depth(mapper, depth_level=[0, 1, 2])

        is equivalent to:

        >>> for level in [0, 1, 2]:
        >>>     index = index.relabel_at_depth(mapper, depth_level=level)

        albeit more efficient.
        '''
        if isinstance(depth_level, INT_TYPES):
            depth_level = [depth_level]
            target_depths = set(depth_level)
        else:
            depth_level = sorted(depth_level)
            target_depths = set(depth_level)

            if len(target_depths) != len(depth_level):
                raise ValueError('depth_levels must be unique')

            if not depth_level:
                raise ValueError('depth_level must be non-empty')

        if any(level < 0 or level >= self.depth for level in depth_level):
            raise ValueError(
                f'Invalid depth level found. Valid levels: [0-{self.depth - 1}]'
            )

        if self._recache:
            self._update_array_cache()

        is_callable = callable(mapper)

        # Special handling for full replacements
        if not is_callable and not hasattr(mapper, 'get'):
            values, _ = iterable_to_array_1d(mapper, count=self.__len__())

            if len(values) != self.__len__():
                raise ValueError('Iterable must provide a value for each label')

            def gen() -> tp.Iterator[np.ndarray]:
                for depth_idx in range(self.depth):
                    if depth_idx in target_depths:
                        yield values
                    else:
                        yield self._blocks._extract_array_column(depth_idx)

            return self.__class__._from_type_blocks(
                    TypeBlocks.from_blocks(gen()),
                    name=self._name,
                    index_constructors=self._index_constructors,
                    own_blocks=True
                    )

        mapper_func = mapper if is_callable else mapper.__getitem__ # type: ignore

        def get_new_label(label: tp.Hashable) -> tp.Hashable:
            if is_callable or label in mapper: # type: ignore
                return mapper_func(label) # type: ignore
            return label

        # depth_level might not contain all depths, so we will re-use our
        # indices/indexers for all those cases.
        new_indices = list(self._indices)
        new_indexers = list(self._indexers)

        for level in depth_level:
            index = self._indices[level]

            new_index: tp.Dict[tp.Hashable, int] = {}
            index_remap: tp.Dict[tp.Hashable, tp.Hashable] = {}

            for label_idx, label in enumerate(index.values):
                new_label = get_new_label(label)

                if new_label not in new_index:
                    new_index[new_label] = len(new_index)
                else:
                    index_remap[label_idx] = new_index[new_label]

            new_indices[level] = index.__class__(new_index)

            indexer = np.array([index_remap.get(i, i) for i in self._indexers[level]])
            indexer.flags.writeable = False
            new_indexers[level] = indexer

        return self.__class__(
                indices=new_indices,
                indexers=new_indexers,
                name=self._name,
                )

    def rehierarch(self: IH,
            depth_map: tp.Sequence[int],
            ) -> IH:
        '''
        Return a new :obj:`IndexHierarchy` that conforms to the new depth assignments given be `depth_map`.
        '''
        if self._recache:
            self._update_array_cache()

        rehierarched_blocks, _ = rehierarch_from_type_blocks(
                labels=self._blocks,
                depth_map=depth_map,
                )
        return self.__class__._from_type_blocks(
            blocks=rehierarched_blocks,
            index_constructors=self._index_constructors,
            own_blocks=True,
            )

    def _get_outer_index_labels_in_order_they_appear(self: IH) -> tp.Sequence[tp.Hashable]:
        '''
        Index could be [A, B, C]
        Indexers could be [2, 0, 0, 2, 1]

        This function return [C, A, B] -- shoutout to my initials
        '''
        # get the outer level, or just the unique frame labels needed
        labels = self.values_at_depth(0)
        label_indexes = ufunc_unique1d_positions(labels)[0]
        label_indexes.sort()
        return labels[label_indexes] # type: ignore

    # --------------------------------------------------------------------------

    def _build_mask_for_key_at_depth(self: IH,
            depth: int,
            key: tp.Tuple[tp.Union[tp.Hashable, slice, np.ndarray, tp.List[tp.Hashable]], ...],
            ) -> np.ndarray:
        '''
        Determines the indexer mask for `key` at `depth`.
        '''
        # Assumes caller has recached!

        key_at_depth = key[depth]

        # Key is already a mask!
        if isinstance(key_at_depth, np.ndarray) and key_at_depth.dtype == DTYPE_BOOL:
            return key_at_depth

        index_at_depth = self._indices[depth]
        indexer_at_depth = self._indexers[depth]

        if isinstance(key_at_depth, slice):
            if key_at_depth.start is not None:
                start: tp.Optional[int] = index_at_depth.loc_to_iloc(key_at_depth.start) # type: ignore
            else:
                start = 0

            if key_at_depth.step is not None and not isinstance(
                key_at_depth.step, INT_TYPES
            ):
                raise TypeError(
                    f'slice step must be an integer, not {type(key_at_depth.step)}'
                )

            if key_at_depth.stop is not None:
                stop: tp.Optional[int] = index_at_depth.loc_to_iloc(key_at_depth.stop) + 1 # type: ignore
            else:
                stop = len(indexer_at_depth)

            if key_at_depth.step is None or key_at_depth.step == 1:
                other = PositionsAllocator.get(stop)[start:]
            else:
                other = np.arange(start, stop, key_at_depth.step)

            return isin_array(
                    array=indexer_at_depth,
                    array_is_unique=False,
                    other=other,
                    other_is_unique=True
                    )

        key_iloc = index_at_depth.loc_to_iloc(key_at_depth)

        if hasattr(key_iloc, '__len__'):
            # Cases where the key is a list of labels
            return isin(indexer_at_depth, key_iloc)

        return indexer_at_depth == key_iloc

    def _loc_to_iloc_index_hierarchy(self: IH,
            key: IH,
            ) -> np.ndarray:
        '''
        Returns the boolean mask for a key that is an IndexHierarchy.

        For small keys, this approach is outperformed by the naive:

            [self._loc_to_iloc(label) for label in key.iter_label()]

        However, this approach quickly outperforms list comprehension as the key gets larger
        '''
        # Assumes caller has recached!

        if not key.depth == self.depth:
            raise KeyError(f'Key must have the same depth as the index. {key}')

        key_indexers: tp.List[np.ndaray] = []
        for key_index, self_index, key_indexer in zip(
                key._indices,
                self._indices,
                key._indexers,
                ):
            indexer_remap = key_index._index_iloc_map(self_index)
            key_indexers.append(indexer_remap[key_indexer])

        self_indexers = np.array(self._indexers, dtype=DTYPE_INT_DEFAULT).T
        key_indexers = np.array(key_indexers, dtype=DTYPE_INT_DEFAULT).T

        ilocs = np.intersect1d(
                view_2d_as_1d(self_indexers),
                view_2d_as_1d(key_indexers),
                assume_unique=True,
                return_indices=True,
                )[1]

        return ilocs

    def _loc_to_iloc_single_key(self: IH,
            key: tp.Tuple[tp.Union[tp.Hashable, slice, np.ndarray, tp.List[tp.Hashable]], ...],
            ) -> tp.Union[int, np.ndarray]:
        '''
        Return the indexer for a given key. Key is assumed to not be compound (i.e. HLoc, list of keys, etc)

        Will return a single integer for single, non-HLoc keys. Otherwise, returns a boolean mask.
        '''
        # Assumes caller has recached!

        # We consider the NULL_SLICE to not be "meaningful", as it requires no filtering
        meaningful_selections = {
                depth: not (isinstance(k, slice) and k == NULL_SLICE)
                for depth, k in enumerate(key)
                }

        if sum(meaningful_selections.values()) == 1:
            # Prefer to avoid construction of a 2D mask

            depth = next(
                i for i, meaningful in meaningful_selections.items() if meaningful
            )
            mask = self._build_mask_for_key_at_depth(depth=depth, key=key)
        else:
            mask_2d = np.full(self.shape, True, dtype=bool)

            for depth, meaningful in meaningful_selections.items():
                if not meaningful:
                    continue

                mask = self._build_mask_for_key_at_depth(depth=depth, key=key)
                mask_2d[:, depth] = mask

            mask = mask_2d.all(axis=1)
            del mask_2d

        result: np.ndarray = self.positions[mask]

        # Even if there was one result, unless the HLoc specified all levels, we need to return a list
        if len(result) == 1:
            if all(
                    meaningful
                    and (isinstance(key[depth], str) or not hasattr(key[depth], '__len__'))
                    for depth, meaningful in meaningful_selections.items()
                    ):
                return result[0]
        return result

    def _loc_to_iloc(self: IH,
            key: tp.Union[GetItemKeyType, HLoc],
            ) -> GetItemKeyType:
        '''
        Given iterable (or instance) of GetItemKeyType, determine the equivalent iloc key.

        When possible, prefer slices.
        '''
        if isinstance(key, ILoc):
            return key.key

        if self._recache:
            self._update_array_cache()

        if isinstance(key, IndexHierarchy):
            return self._loc_to_iloc_index_hierarchy(key)

        if isinstance(key, np.ndarray) and key.dtype == DTYPE_BOOL:
            return self.positions[key]

        if isinstance(key, slice):
            return slice(*LocMap.map_slice_args(self._loc_to_iloc, key))

        if isinstance(key, list):
            return [self._loc_to_iloc(k) for k in key]

        if isinstance(key, np.ndarray) and key.ndim == 2:
            if key.dtype != DTYPE_OBJECT:
                return np.intersect1d(
                        view_2d_as_1d(self.values),
                        view_2d_as_1d(key),
                        assume_unique=False,
                        return_indices=True,
                        )[1]

            return [self._loc_to_iloc(k) for k in key]

        if isinstance(key, HLoc):
            # unpack any Series, Index, or ILoc into the context of this IndexHierarchy
            key = tuple(HLoc(tuple(key_from_container_key(self, k, expand_iloc=True) for k in key)))

            if len(key) > self.depth:
                raise RuntimeError(
                    f'Too many depths specified for {key}. Expected: {self.depth}'
                )
        else:
            # If the key is a series, key_from_container_key will invoke IndexCorrespondence
            # logic that eventually calls _loc_to_iloc on all the indices of that series.
            sanitized_key = key_from_container_key(self, key)

            if key is sanitized_key:
                # This is always either a tuple, or a 1D numpy array
                if len(key) != self.depth:
                    raise RuntimeError(
                        f'Invalid key length for {key}; must be length {self.depth}.'
                    )

                if any(isinstance(subkey, KEY_MULTIPLE_TYPES) for subkey in key): # type: ignore
                    raise RuntimeError(
                        f'slices cannot be used in a leaf selection into an IndexHierarchy; try HLoc[{key}].'
                    )

            else:
                key = sanitized_key
                if isinstance(key, np.ndarray) and key.dtype == DTYPE_BOOL:
                    # When the key is a series with boolean values
                    return self.positions[key]

                key = tuple(key)

                for subkey in key:
                    if len(subkey) != self.depth:
                        raise RuntimeError(
                            f'Invalid key length for {subkey}; must be length {self.depth}.'
                        )

        if any(isinstance(k, tuple) for k in key): # type: ignore
            # We can occasionally receive a sequence of tuples
            return [self._loc_to_iloc(k) for k in key] # type: ignore

        return self._loc_to_iloc_single_key(key)

    def loc_to_iloc(self: IH,
            key: tp.Union[GetItemKeyType, HLoc],
            ) -> GetItemKeyType:
        '''
        Given a label (loc) style key (either a label, a list of labels, a slice, an HLoc object, or a Boolean selection), return the index position (iloc) style key. Keys that are not found will raise a KeyError or a sf.LocInvalid error.

        Args:
            key: a label key.
        '''
        # NOTE: the public method is the same as the private method for IndexHierarchy, but not for Index
        return self._loc_to_iloc(key)

    def _extract_iloc(self: IH,
            key: GetItemKeyType,
            ) -> tp.Union[IH, SingleLabelType]:
        '''
        Extract a new index given an iloc key
        '''
        if self._recache:
            self._update_array_cache()

        if isinstance(key, INT_TYPES):
            # return a tuple if selecting a single row
            # NOTE: Selecting a single row may force type coercion before values are added to the tuple; i.e., a datetime64 will go to datetime.date before going to the tuple
            return tuple(self._blocks._extract_array(row_key=key))

        if key is None:
            return self.copy()

        tb = self._blocks._extract(row_key=key)
        new_indices: tp.List[Index] = []
        new_indexers: tp.List[np.ndarray] = []

        for index, indexer in zip(self._indices, self._indexers):
            unique_indexes, new_indexer = ufunc_unique1d_indexer(indexer[key])

            new_indices.append(index.iloc[unique_indexes])
            new_indexers.append(new_indexer)

        return self.__class__(
                indices=new_indices,
                indexers=new_indexers,
                name=self.name,
                blocks=tb,
                own_blocks=True,
                )

    def _extract_loc(self: IH,
            key: GetItemKeyType,
            ) -> tp.Union[IH, SingleLabelType]:
        '''
        Extract a new index given an loc key
        '''
        return self._extract_iloc(self._loc_to_iloc(key))

    def __getitem__(self: IH,
            key: GetItemKeyType,
            ) -> tp.Union[IH, SingleLabelType]:
        '''
        Extract a new index given a key.
        '''
        return self._extract_iloc(key)

    # --------------------------------------------------------------------------

    def _extract_getitem_astype(self: IH,
            key: GetItemKeyType,
            ) -> 'IndexHierarchyAsType':
        '''
        Given an iloc key (using integer positions for columns) return a configured IndexHierarchyAsType instance.
        '''
        # key is an iloc key
        if isinstance(key, tuple):
            raise KeyError('__getitem__ does not support multiple indexers')

        return IndexHierarchyAsType(self, key=key)

    # --------------------------------------------------------------------------
    # operators

    def _ufunc_unary_operator(self: IH,
            operator: UFunc,
            ) -> np.ndarray:
        '''
        Always return an NP array.
        '''
        if self._recache:
            self._update_array_cache()

        array = operator(self.values)
        array.flags.writeable = False
        return array

    def _ufunc_binary_operator(self: IH,
            *,
            operator: UFunc,
            other: tp.Any,
            axis: int = 0,
            fill_value: object = np.nan,
            ) -> np.ndarray:
        '''
        Binary operators applied to an index always return an NP array. This deviates from Pandas, where some operations (multiplying an int index by an int) result in a new Index, while other operations result in a np.array (using == on two Index).
        '''
        from static_frame.core.series import Series
        from static_frame.core.frame import Frame

        if isinstance(other, (Series, Frame)):
            raise ValueError('cannot use labelled container as an operand.')

        if self._recache:
            self._update_array_cache()

        if operator.__name__ == 'matmul':
            return matmul(self._blocks.values, other)
        elif operator.__name__ == 'rmatmul':
            return matmul(other, self._blocks.values)

        if isinstance(other, Index):
            other = other.values
        elif isinstance(other, IndexHierarchy):
            other = other._blocks

        tb = self._blocks._ufunc_binary_operator(
                    operator=operator,
                    other=other,
                    axis=axis,
                    )
        return tb.values

    def _ufunc_axis_skipna(self: IH,
            *,
            axis: int,
            skipna: bool,
            ufunc: UFunc,
            ufunc_skipna: UFunc,
            composable: bool,
            dtypes: tp.Tuple[np.dtype, ...],
            size_one_unity: bool,
            ) -> np.ndarray:
        '''
        Returns:
            immutable NumPy array.
        '''
        if self._recache:
            self._update_array_cache()

        dtype = None if not dtypes else dtypes[0] # must be a tuple
        values = self._blocks.values

        if skipna:
            post = ufunc_skipna(values, axis=axis, dtype=dtype)
        else:
            post = ufunc(values, axis=axis, dtype=dtype)

        post.flags.writeable = False
        return post

    # _ufunc_shape_skipna defined in IndexBase

    # --------------------------------------------------------------------------
    # dictionary-like interface

    # NOTE: we intentionally exclude keys(), items(), and get() from Index classes, as they return inconsistent result when thought of as a dictionary

    def __iter__(self: IH) -> tp.Iterator[SingleLabelType]:
        '''
        Iterate over labels.
        '''
        # Don't use .values, as that can coerce types
        yield from zip(*map(self.values_at_depth, range(self.depth)))

    def __reversed__(self: IH) -> tp.Iterator[SingleLabelType]:
        '''
        Returns a reverse iterator on the index labels.
        '''
        if self._recache:
            self._update_array_cache()

        for array in self._blocks.axis_values(1, reverse=True):
            yield tuple(array)

    def __contains__(self: IH,
            value: tp.Hashable,
            ) -> bool:
        '''
        Determine if a label `value` is contained in this Index.
        '''
        try:
            result = self._loc_to_iloc(value)
        except KeyError:
            return False

        if isinstance(result, (np.ndarray, list)):
            return bool(len(result))

        return True

    # --------------------------------------------------------------------------
    # utility functions

    def unique(self: IH,
            depth_level: DepthLevelSpecifier = 0,
            ) -> np.ndarray:
        '''
        Return a NumPy array of unique values.

        Args:
            depth_level: Specify a single depth or multiple depths in an iterable.

        Returns:
            :obj:`numpy.ndarray`
        '''
        pos: tp.Optional[int] = None
        if not isinstance(depth_level, INT_TYPES):
            sel = list(depth_level)
            if len(sel) == 1:
                pos = sel.pop()
        else: # is an int
            pos = depth_level

        if pos is not None: # i.e. a single level
            return self._indices[pos].values

        return ufunc_unique(array2d_to_array1d(self.values_at_depth(sel)))

    @doc_inject()
    def equals(self: IH,
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
        # NOTE: do not need to udpate array cache, as can compare elements in levels
        if id(other) == id(self):
            return True

        if compare_class and self.__class__ != other.__class__:
            return False

        if not isinstance(other, IndexHierarchy):
            return False

        # same type from here
        if self.shape != other.shape:
            return False

        if compare_name and self.name != other.name:
            return False

        if compare_dtype and not self.dtypes.equals(other.dtypes):
            return False

        if compare_class:
            for self_index, other_index in zip(self._indices, other._indices):
                if self_index.__class__ != other_index.__class__:
                    return False

        # indices & indexers are encoded in values_at_depth
        for i in range(self.depth):
            if not arrays_equal(
                    self.values_at_depth(i),
                    other.values_at_depth(i),
                    skipna=skipna,
                    ):
                return False

        return True

    @doc_inject(selector='sort')
    def sort(self: IH,
            *,
            ascending: BoolOrBools = True,
            kind: str = DEFAULT_SORT_KIND,
            key: tp.Optional[tp.Callable[[IH], tp.Union[np.ndarray, IH]]] = None,
            ) -> IH:
        '''
        Return a new Index with the labels sorted.

        Args:
            {ascendings}
            {kind}
            {key}
        '''
        if self._recache:
            self._update_array_cache()

        order = sort_index_for_order(self, kind=kind, ascending=ascending, key=key)

        blocks = self._blocks._extract(row_key=order)

        return self.__class__._from_type_blocks(
                blocks=blocks,
                index_constructors=self._index_constructors,
                name=self._name,
                own_blocks=True,
                )

    def isin(self: IH,
            other: tp.Iterable[tp.Iterable[tp.Hashable]],
            ) -> np.ndarray:
        '''
        Return a Boolean array showing True where one or more of the passed in iterable of labels is found in the index.
        '''
        matches = []
        for seq in other:
            if not hasattr(seq, '__iter__'):
                raise RuntimeError(
                    'must provide one or more iterables within an iterable'
                )
            # Coerce to hashable type
            as_tuple = tuple(seq)
            if len(as_tuple) == self.depth:
                # can pre-filter if iterable matches to length
                matches.append(as_tuple)

        if not matches:
            return np.full(self.__len__(), False, dtype=bool)

        if self._recache:
            self._update_array_cache()

        return isin(self.flat().values, matches)

    def roll(self: IH,
            shift: int,
            ) -> IH:
        '''
        Return an :obj:`IndexHierarchy` with values rotated forward and wrapped around (with a positive shift) or backward and wrapped around (with a negative shift).
        '''
        if self._recache:
            self._update_array_cache()

        blocks = TypeBlocks.from_blocks(
                self._blocks._shift_blocks(row_shift=shift, wrap=True)
                )

        return self.__class__._from_type_blocks(
                blocks=blocks,
                index_constructors=self._index_constructors,
                name=self._name,
                own_blocks=True,
                )

    @doc_inject(selector='fillna')
    def fillna(self: IH,
            value: tp.Any,
            ) -> IH:
        '''
        Return an :obj:`IndexHierarchy` after replacing NA (NaN or None) with the supplied value.

        Args:
            {value}
        '''
        if self._recache:
            self._update_array_cache()

        blocks = self._blocks.fill_missing_by_unit(value, None, func=isna_array)

        return self.__class__._from_type_blocks(
                blocks=blocks,
                index_constructors=self._index_constructors,
                name=self._name,
                own_blocks=True,
                )

    def _sample_and_key(self: IH,
            count: int = 1,
            *,
            seed: tp.Optional[int] = None,
            ) -> tp.Tuple[IH, np.ndarray]:
        '''
        Selects a deterministically random sample from this IndexHierarchy.

        Returns:
            The sampled IndexHierarchy
            An integer array of sampled iloc values
        '''
        if self._recache:
            self._update_array_cache()

        key = array_sample(self.positions, count=count, seed=seed, sort=True)
        blocks = self._blocks._extract(row_key=key)

        container = self.__class__._from_type_blocks(
                blocks=blocks,
                index_constructors=self._index_constructors,
                name=self._name,
                own_blocks=True,
                )
        return container, key

    @doc_inject(selector='searchsorted', label_type='iloc (integer)')
    def iloc_searchsorted(self: IH,
            values: tp.Any,
            *,
            side_left: bool = True,
            ) -> tp.Union[tp.Hashable, tp.Sequence[tp.Hashable]]:
        '''
        {doc}

        Args:
            {values}
            {side_left}
        '''
        if isinstance(values, tuple):
            match_pre = [values] # normalize a multiple selection
            is_element = True
        elif isinstance(values, list):
            match_pre = values
            is_element = False
        else:
            raise NotImplementedError(
                'A single label (as a tuple) or multiple labels (as a list) must be provided.'
            )

        dt_pos = np.array(
            [
                issubclass(idx_type, IndexDatetime)
                for idx_type in self._index_constructors
            ]
        )
        has_dt = dt_pos.any()

        values_for_match = np.empty(len(match_pre), dtype=object)

        for i, label in enumerate(match_pre):
            if has_dt:
                label = tuple(
                    v if not dt_pos[j] else key_to_datetime_key(v)
                    for j, v in enumerate(label)
                )
            values_for_match[i] = label

        post: np.ndarray = self.flat().iloc_searchsorted(values_for_match, side_left=side_left)
        if is_element:
            return tp.cast(tp.Hashable, post[0])
        return tp.cast(tp.Sequence[tp.Hashable], post)

    @doc_inject(selector='searchsorted', label_type='loc (label)')
    def loc_searchsorted(self: IH,
            values: tp.Any,
            *,
            side_left: bool = True,
            fill_value: tp.Any = np.nan,
            ) -> tp.Union[tp.Hashable, tp.Iterable[tp.Hashable], tp.Any]:
        '''
        {doc}

        Args:
            {values}
            {side_left}
            {fill_value}
        '''
        # will return an integer or an array of integers
        sel = self.iloc_searchsorted(values, side_left=side_left)

        length = self.__len__()
        if sel.ndim == 0 and sel == length: # an element:
            return fill_value

        flat = self.flat().values
        mask = sel == length
        if not mask.any():
            return flat[sel]

        post = np.empty(len(sel), dtype=object)
        sel[mask] = 0 # set out of range values to zero
        post[:] = flat[sel]
        post[mask] = fill_value
        post.flags.writeable = False
        return post

    # --------------------------------------------------------------------------
    # export

    def _to_frame(self: IH,
            constructor: tp.Type['Frame'],
            ) -> 'Frame':
        if self._recache:
            self._update_array_cache()

        return constructor(
                self._blocks.copy(),
                columns=None,
                index=None,
                own_data=True,
                )

    def to_frame(self: IH) -> 'Frame':
        '''
        Return :obj:`Frame` version of this :obj:`IndexHierarchy`.
        '''
        from static_frame import Frame
        return self._to_frame(Frame)

    def to_frame_go(self: IH) -> 'FrameGO':
        '''
        Return a :obj:`FrameGO` version of this :obj:`IndexHierarchy`.
        '''
        from static_frame import FrameGO
        return tp.cast(FrameGO, self._to_frame(FrameGO))

    def to_pandas(self: IH) -> 'DataFrame':
        '''
        Return a Pandas MultiIndex.
        '''
        import pandas

        if self._recache:
            self._update_array_cache()

        # must copy to get a mutable array
        mi = pandas.MultiIndex(
                levels=[index.values.copy() for index in self._indices],
                codes=[arr.copy() for arr in self._indexers],
                )
        mi.name = self._name
        mi.names = self.names
        return mi

    def _build_tree_at_depth_from_mask(self: IH,
            depth: int,
            mask: np.ndarray,
            ) -> tp.Union[TreeNodeT, Index]:
        '''
        Recursively build a tree of :obj:`TreeNodeT` at `depth` given `mask`
        '''
        # Assumes caller has recached!

        if depth == self.depth - 1:
            values = self._indices[depth][self._indexers[depth][mask]]
            return self._indices[depth].__class__(values)

        tree: TreeNodeT = {}

        index_at_depth = self._indices[depth]
        indexer_at_depth = self._indexers[depth]

        for i in ufunc_unique(indexer_at_depth[mask]):
            tree[index_at_depth[i]] = self._build_tree_at_depth_from_mask(
                    depth=depth + 1,
                    mask=mask & (indexer_at_depth == i),
                    )

        return tree

    def to_tree(self: IH) -> TreeNodeT:
        '''
        Returns the tree representation of an IndexHierarchy
        '''
        if self._recache:
            self._update_array_cache()

        tree = self._build_tree_at_depth_from_mask(
                depth=0,
                mask=np.ones(self.__len__(), dtype=bool),
                )
        return tree # type: ignore

    def flat(self: IH) -> Index:
        '''
        Return a flat, one-dimensional index of tuples for each level.
        '''
        return self._INDEX_CONSTRUCTOR(self.__iter__(), name=self._name) # type: ignore

    def level_add(self: IH,
            level: tp.Hashable,
            *,
            index_constructor: IndexConstructor = None,
            ) -> IH:
        '''
        Return an IndexHierarchy with a new root (outer) level added.
        '''
        if self._recache:
            self._update_array_cache()

        index_cls = self._INDEX_CONSTRUCTOR if index_constructor is None else index_constructor._MUTABLE_CONSTRUCTOR # type: ignore

        if self.STATIC:
            indices = [index_cls((level,)), *self._indices]
        else:
            indices = [index_cls((level,)), *(idx.copy() for idx in self._indices)]

        # Indexers are always immutable
        new_indexer = np.zeros(self.__len__(), dtype=int)
        new_indexer.flags.writeable = False
        indexers = [new_indexer, *self._indexers]

        def gen_blocks() -> tp.Iterator[np.ndarray]:
            yield np.full(self.__len__(), indices[0].values)
            yield from self._blocks._blocks

        return self.__class__(
                indices=indices,
                indexers=indexers,
                name=self.name,
                blocks=TypeBlocks.from_blocks(gen_blocks()),
                own_blocks=True,
                )

    def level_drop(self: IH,
            count: int = 1,
            ) -> tp.Union[Index, IH]:
        '''
        Return an IndexHierarchy with one or more leaf levels removed.

        Args:
            count: A positive value is the number of depths to remove from the root (outer) side of the hierarchy; a negative value is the number of depths to remove from the leaf (inner) side of the hierarchy.
        '''
        if self._recache:
            self._update_array_cache()

        # NOTE: this was implement with a bipolar ``count`` to specify what to drop, but it could have been implemented with a depth level specifier, supporting arbitrary removals. The approach taken here is likely faster as we reuse levels.
        if self._name_is_names():
            if count < 0:
                name: NameType = self._name[:count] # type: ignore
            elif count > 0:
                name = self._name[count:] # type: ignore
            if len(name) == 1:
                name = name[0] # type: ignore
        else:
            name = self._name

        if count < 0:
            if count <= (1 - self.depth):
                # When the removal equals or exceeds the depth of the hierarchy, we just return the outermost index
                # NOTE: We can't copy the index directly, since the index order might not match.
                return self._indices[0].__class__(
                        self._blocks.iloc[:,0].values.reshape(self.__len__()),
                        name=name,
                        )

            # Remove from inner
            return self.__class__(
                    indices=self._indices[:count],
                    indexers=self._indexers[:count],
                    name=name,
                    blocks=self._blocks[:count],
                    own_blocks=self.STATIC,
                    )

        elif count > 0:
            if count >= (self.depth - 1):
                # When the removal equals or exceeds the depth of the hierarchy, we just return the innermost index
                # NOTE: We can't copy the index directly, since the index order might not match.
                return self._indices[-1].__class__(
                        self._blocks.iloc[:,-1].values.reshape(self.__len__()),
                        name=name,
                        )

            # Remove from outer
            return self.__class__(
                    indices=self._indices[count:],
                    indexers=self._indexers[count:],
                    name=name,
                    blocks=self._blocks.iloc[:,count:],
                    own_blocks=self.STATIC,
                    )

        raise NotImplementedError('no handling for a 0 count drop level.')


class IndexHierarchyGO(IndexHierarchy):
    '''
    A hierarchy of :obj:`static_frame.Index` objects that permits mutation only in the addition of new hierarchies or labels.
    '''

    STATIC = False

    _IMMUTABLE_CONSTRUCTOR = IndexHierarchy
    _INDEX_CONSTRUCTOR = IndexGO

    __slots__ = (
            '_indices',
            '_indexers',
            '_name',
            '_blocks',
            '_recache',
            '_index_type',
            )

    _indices: tp.List[IndexGO] # type: ignore

    def _append(self: IH,
            row: SingleLabelType,
            indexers: tp.List[np.ndarray],
            offset: int,
            ) -> None:
        for depth, label_at_depth in enumerate(row):
            label_index = self._indices[depth]._loc_to_iloc(label_at_depth)
            indexers[depth][offset] = label_index

    def _extend(self: IH,
            other: IH,
            indexers: tp.List[np.ndarray],
            offset: int,
            size: int,
            ) -> None:
        '''
        Extend this IndexHierarchy in-place
        '''
        for depth, (self_index, other_index) in enumerate(
                zip(self._indices, other._indices)
                ):

            # We have already grown all the indices, so all labels in `other` exist in `self`
            # We just need to remap them
            remapped_indexers_unordered = other_index._index_iloc_map(self_index)
            remapped_indexers_ordered = remapped_indexers_unordered[other._indexers[depth]]

            indexers[depth][offset:offset+size] = remapped_indexers_ordered

    def append(self: IHGO,
            value: tp.Sequence[tp.Hashable],
            ) -> None:
        '''
        Append a single label to this index.
        '''
        # We do not check whether nor the key exists, as that is too expensive.
        # Instead, we delay failure until _recache

        for depth, label_at_depth in enumerate(value):
            if label_at_depth not in self._indices[depth]:
                self._indices[depth].append(label_at_depth)

        if self._pending_extensions is None:
            self._pending_extensions = []

        self._pending_extensions.append(PendingRow(value))
        self._recache = True

    def extend(self: IHGO,
            other: IndexHierarchy,
            ) -> None:
        '''
        Extend this IndexHierarchy in-place
        '''
        # We do not check whether nor the key exists, as that is too expensive.
        # Instead, we delay failure until _recache

        for self_index, other_index in zip(self._indices, other._indices):
            # This will force a recache -> but that's okay.
            difference = other_index.difference(self_index)
            if difference.size:
                self_index.extend(difference)

        if self._pending_extensions is None:
            self._pending_extensions = []

        self._pending_extensions.append(other)
        self._recache = True

    def __copy__(self: IHGO) -> IHGO:
        '''
        Return a shallow copy of this IndexHierarchy.
        '''
        if self._recache:
            self._update_array_cache()

        return self.__class__(
                indices=[index.copy() for index in self._indices],
                indexers=list(self._indexers),
                name=self._name,
                blocks=self._blocks.copy(),
                own_blocks=True,
                )


# update class attr on Index after class initialization
IndexHierarchy._MUTABLE_CONSTRUCTOR = IndexHierarchyGO


class IndexHierarchyAsType:

    __slots__ = (
            'container',
            'key',
            )

    container: IndexHierarchy
    key: GetItemKeyType

    def __init__(self: IHAsType,
            container: IndexHierarchy,
            key: GetItemKeyType
            ) -> None:
        self.container = container
        self.key = key

    def __call__(self: IHAsType,
            dtype: DtypeSpecifier,
            ) -> IndexHierarchy:
        '''
        Entrypoint to `astype` the container
        '''
        from static_frame.core.index_datetime import dtype_to_index_cls
        container = self.container

        # use TypeBlocks in both situations to avoid double casting
        blocks = TypeBlocks.from_blocks(
                container._blocks._astype_blocks(column_key=self.key, dtype=dtype)
                )

        # avoid coercion of datetime64 arrays that were not targetted in the selection
        index_constructors = container.index_types.values.copy()

        dtype_post = blocks.dtypes[self.key] # can select element or array
        if isinstance(dtype_post, np.dtype):
            index_constructors[self.key] = dtype_to_index_cls(
                    container.STATIC,
                    dtype_post,
                    )
        else: # assign iterable
            index_constructors[self.key] = [
                dtype_to_index_cls(container.STATIC, dt) for dt in dtype_post
            ]

        return container.__class__._from_type_blocks(
                blocks=blocks,
                index_constructors=index_constructors,
                own_blocks=True,
                )
