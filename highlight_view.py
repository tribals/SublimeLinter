from collections import defaultdict, ChainMap
from contextlib import contextmanager
import html
from itertools import chain
from functools import partial
import re
import textwrap
import uuid

import sublime
import sublime_plugin

from .lint import persist, events, style, util, queue, quick_fix
from .lint.const import PROTECTED_REGIONS_KEY, ERROR, WARNING
flatten = chain.from_iterable


MYPY = False
if MYPY:
    from typing import (
        Callable, DefaultDict, Dict, FrozenSet, Hashable, Iterable, List,
        Mapping, Optional, Set, Tuple, TypeVar, Union
    )
    from mypy_extensions import TypedDict
    T = TypeVar('T')
    LintError = persist.LintError
    LinterName = persist.LinterName

    Flags = int
    Icon = str
    Scope = str
    Squiggles = Mapping['Squiggle', List[sublime.Region]]
    GutterIcons = Mapping['GutterIcon', List[sublime.Region]]
    ProtectedRegions = List[sublime.Region]
    RegionKey = Union['GutterIcon', 'Squiggle']

    State_ = TypedDict('State_', {
        'active_view': Optional[sublime.View],
        'current_sel': Tuple[sublime.Region, ...],
        'idle_views': Set[sublime.ViewId],
        'quiet_views': Set[sublime.ViewId],
        'views_without_phantoms': Set[sublime.ViewId],
        'views': Set[sublime.ViewId]
    })

    DemotePredicate = Callable[[LintError], bool]
    FilteredErrors = Tuple[List[LintError], List[LintError]]


UNDERLINE_FLAGS = (
    sublime.DRAW_NO_FILL |
    sublime.DRAW_NO_OUTLINE
)

MARK_STYLES = {
    'outline': sublime.DRAW_NO_FILL,
    'fill': sublime.DRAW_NO_OUTLINE,
    'solid_underline': sublime.DRAW_SOLID_UNDERLINE | UNDERLINE_FLAGS,
    'squiggly_underline': sublime.DRAW_SQUIGGLY_UNDERLINE | UNDERLINE_FLAGS,
    'stippled_underline': sublime.DRAW_STIPPLED_UNDERLINE | UNDERLINE_FLAGS,
    'none': sublime.HIDDEN
}
UNDERLINE_STYLES = (
    'solid_underline', 'squiggly_underline', 'stippled_underline'
)

SOME_WS = re.compile(r'\s')
FALLBACK_MARK_STYLE = 'outline'

WS_ONLY = re.compile(r'^\s+$')
MULTILINES = re.compile('(?s)\n(?=.)')

# Sublime >= 4074 supports underline styles on white space
# https://github.com/sublimehq/sublime_text/issues/137
SUBLIME_SUPPORTS_WS_SQUIGGLES = int(sublime.version()) >= 4074
SUBLIME_SUPPORTS_REGION_ANNOTATIONS = int(sublime.version()) >= 4050

State = {
    'active_view': None,
    'current_sel': tuple(),
    'idle_views': set(),
    'quiet_views': set(),
    'views_without_phantoms': set(),
    'views': set()
}  # type: State_


def plugin_loaded():
    State.update({
        'active_view': sublime.active_window().active_view(),
        'idle_views': set()
    })


def plugin_unloaded():
    events.off(on_lint_result)
    for window in sublime.windows():
        for view in window.views():
            undraw(view)


@events.on(events.LINT_RESULT)
def on_lint_result(filename, linter_name, **kwargs):
    # type: (str, LinterName, object) -> None
    views = list(all_views_into_file(filename))
    if not views:
        return

    highlight_linter_errors(views, filename, linter_name)


class UpdateOnLoadController(sublime_plugin.EventListener):
    def on_load_async(self, view):
        # type: (sublime.View) -> None
        # update this new view with any errors it currently has
        filename = util.canonical_filename(view)
        errors = persist.file_errors.get(filename)
        if errors:
            set_idle(view, True)  # show errors immediately
            linter_names = set(error['linter'] for error in errors)
            for linter_name in linter_names:
                highlight_linter_errors([view], filename, linter_name)

    on_clone_async = on_load_async


def highlight_linter_errors(views, filename, linter_name):
    # type: (List[sublime.View], str, LinterName) -> None
    demote_predicate = get_demote_predicate()
    demote_scope = get_demote_scope()

    errors = persist.file_errors[filename]
    update_error_priorities_inline(errors)
    errors_for_the_highlights, loosers = filter_errors(errors, by_position)
    errors_for_the_gutter, _ = filter_errors(errors, by_line)

    gutter_regions = prepare_gutter_data(errors_for_the_gutter)

    for view in views:
        vid = view.id()

        if vid not in State['views']:
            if persist.settings.get('highlights.start_hidden'):
                State['quiet_views'].add(vid)
                State['views_without_phantoms'].add(vid)

            State['views'].add(vid)

        highlight_regions = prepare_highlights_data(
            errors_for_the_highlights,
            demote_predicate=demote_predicate,
            demote_scope=demote_scope,
            quiet=vid in State['quiet_views'],
            idle=vid in State['idle_views']
        )
        hidden_highlight_regions = prepare_highlights_data(
            loosers,
            demote_predicate=demote_predicate,
            demote_scope=demote_scope,
            quiet=True,
            idle=vid in State['idle_views']
        )
        squiggle_regions = ChainMap(
            {}, highlight_regions, hidden_highlight_regions  # type: ignore[arg-type]
        )  # type: Squiggles

        draw(view, linter_name, squiggle_regions, gutter_regions)
        draw_phantoms(view)


def draw_phantoms(view):
    vid = view.id()
    filename = util.canonical_filename(view)
    errors = persist.file_errors[filename]
    phantoms = (
        prepare_phantoms(view, errors)
        if vid not in State['views_without_phantoms']
        else []
    )
    update_phantoms(view, phantoms)


@util.ensure_on_ui_thread
def update_phantoms(view, phantoms):
    with stable_viewport(view, phantoms):
        get_phantom_set(view).update(phantoms)


@contextmanager
def stable_viewport(view, phantoms):
    pos = cur_pos(view)
    offset = y_offset(view, pos.a)

    yield

    _, cy = view.text_to_layout(pos.a)
    vy = cy - offset
    vx, _ = view.viewport_position()
    view.set_viewport_position((vx, vy), animate=False)


def cur_pos(view):
    # type: (sublime.View) -> sublime.Region
    return view.sel()[0]


def y_offset(view, cursor):
    # type: (sublime.View, int) -> float
    _, cy = view.text_to_layout(cursor)
    _, vy = view.viewport_position()
    return cy - vy


phantoms_per_buffer = {}  # type: Dict[sublime.BufferId, sublime.PhantomSet]

PHANTOM_TEMPLATE = '''
    <body id="sl-inline-phantom">
        <style>
            body {{
                padding: 0rem;
                margin: 0rem;
            }}
            div.error {{
                padding: 0rem;
                margin: 0rem;
                color: {color};
                background-color: color({color} alpha(0.2));
            }}
        </style>
        <div class="error">{content}</div>
    </body>
'''


def get_phantom_set(view):
    # type: (sublime.View) -> sublime.PhantomSet
    bid = view.buffer_id()
    try:
        return phantoms_per_buffer[bid]
    except LookupError:
        rv = phantoms_per_buffer[bid] = sublime.PhantomSet(view, "SLInlineHighlighter")
        return rv


def format_message_for_phantom(view, error):
    col = error["start"]
    vx, _ = view.viewport_extent()
    # `40` *is* a magic number but be sure to never get a `-1` here
    viewport_width = max(40, int(vx // view.em_width()) - 1)
    ralign = col > viewport_width * 2 // 3
    rv = list(flatten(
        textwrap.wrap(
            msg_line,
            width=viewport_width,
            initial_indent=" " * (col if n == 0 and not ralign else 0),
            subsequent_indent=" " * ((col + 2) if not ralign else 0)
        )
        for n, msg_line in enumerate(
            style
            .get_value('phantom', error, '')
            .format(**error)
            .splitlines()
        )
    ))

    if ralign:
        left_spaces = " " * (col - len(rv[0]) - 2)
        rv = [left_spaces + rv[0] + " /"] + [left_spaces + line for line in rv[1:]]

    else:
        rv[0] = (
            " " * col + "\\ " + rv[0].lstrip()
            + " " * (viewport_width - col - 2 - len(rv[0].lstrip()))
        )

    text = (
        html.escape("\n".join(rv), quote=False)
        .replace(' ', '&nbsp;')
        .replace("\n", "<br/>")
    )
    scope = style.get_value('scope', error)
    return PHANTOM_TEMPLATE.format(
        content=text,
        color=view.style_for_scope(scope)["foreground"]
    )


def prepare_phantoms(view, errors):
    errors_ = [e for e in errors if e["error_type"] == "error"]
    if any(errors_):
        errors = errors_

    return [
        sublime.Phantom(
            sublime.Region(error["region"].b - 1),
            format_message_for_phantom(view, error),
            sublime.LAYOUT_BLOCK
        )
        for error in errors
        if style.get_value('phantom', error, '')
    ]


def update_error_priorities_inline(errors):
    # type: (List[LintError]) -> None
    # We need to update `priority` here (although a user will rarely change
    # this setting that often) for correctness. Generally, on views with
    # multiple linters running, we compare new lint results from the
    # 'fast' linters with old results from the 'slower' linters. The below
    # `filter_errors` produces wrong results with outdated priorities.
    #
    # ATT: inline, so this change propagates throughout the system
    for error in errors:
        error['priority'] = style.get_value('priority', error, 0)


def filter_errors(errors, group_fn):
    # type: (List[LintError], Callable[[LintError], Hashable]) -> FilteredErrors
    grouped = defaultdict(list)  # type: DefaultDict[Hashable, List[LintError]]
    for error in errors:
        grouped[group_fn(error)].append(error)

    filtered_errors = []
    loosers = []
    for errors in grouped.values():
        head, *tail = sorted(
            errors,
            key=lambda e: (-e['priority'], e['error_type'], e['linter'])
        )
        filtered_errors.append(head)
        loosers += tail

    return filtered_errors, loosers


def by_position(error):
    # type: (LintError) -> Hashable
    return error['line'], error['start'], error['region'].end()


def by_line(error):
    # type: (LintError) -> Hashable
    return error['line']


def prepare_gutter_data(
    errors,        # type: List[LintError]
):
    # type: (...) -> GutterIcons
    # Compute the icon and scope for the gutter mark from the error.
    # Drop lines for which we don't get a value or for which the user
    # specified 'none'
    by_key = defaultdict(list)
    for error in errors:
        icon = style.get_icon(error)
        if icon == 'none':
            continue

        scope = style.get_icon_scope(error)
        # We draw gutter icons with `flag=sublime.HIDDEN`. The actual width
        # of the region doesn't matter bc Sublime will draw an icon only
        # on the beginning line, which is exactly what we want.
        region = error['region']

        # We group towards the optimal sublime API usage:
        #   view.add_regions(uuid(), [region], scope, icon)
        linter_name = error['linter']
        key = GutterIcon(linter_name, scope, icon)
        by_key[key].append(region)

    return by_key


def prepare_highlights_data(
    errors,            # type: List[LintError]
    demote_predicate,  # type: DemotePredicate
    demote_scope,      # type: str
    quiet,             # type: bool
    idle,              # type: bool
):
    # type: (...) -> Squiggles
    by_region_id = {}
    for error in errors:
        if error.get('revalidate'):
            continue
        scope = style.get_value('scope', error)
        flags = _compute_flags(error)
        demote_while_busy = demote_predicate(error)

        alt_scope = scope
        if quiet:
            scope = HIDDEN_SCOPE
        elif not idle and demote_while_busy:
            scope = demote_scope

        uid = error['uid']
        linter_name = error['linter']
        annotation = style.get_value('annotation', error, '').format(**error)
        key = Squiggle(linter_name, uid, scope, flags, demote_while_busy, alt_scope, annotation=annotation)
        by_region_id[key] = [error['region']]

    return by_region_id


def _compute_flags(error):
    # type: (LintError) -> int
    mark_style = style.get_value('mark_style', error, 'none')
    selected_text = error['offending_text']
    if SUBLIME_SUPPORTS_WS_SQUIGGLES:
        regex = MULTILINES
    else:
        regex = SOME_WS
    if mark_style in UNDERLINE_STYLES and regex.search(selected_text):
        mark_style = FALLBACK_MARK_STYLE

    if (
        mark_style == 'none'
        and style.get_value('annotation', error, '')
    ):
        flags = -1
    else:
        flags = MARK_STYLES[mark_style]
    if not persist.settings.get('show_marks_in_minimap'):
        flags |= sublime.HIDE_ON_MINIMAP
    if error['region'].empty():
        flags |= sublime.DRAW_EMPTY_AS_OVERWRITE
    return flags


def undraw(view):
    # type: (sublime.View) -> None
    for key in get_regions_keys(view):
        erase_view_region(view, key)


@util.ensure_on_ui_thread
def draw(
    view,               # type: sublime.View
    linter_name,        # type: LinterName
    highlight_regions,  # type: Squiggles
    gutter_regions,     # type: GutterIcons
):
    # type: (...) -> None
    """
    Draw code and gutter marks in the given view.

    Error, warning and gutter marks are drawn with separate regions,
    since each one potentially needs a different color.

    """
    current_region_keys = get_regions_keys(view)
    next_region_keys = highlight_regions.keys() | gutter_regions.keys()

    # remove unused regions
    for key in current_region_keys - next_region_keys:
        erase_view_region(view, key)

    # overlaying all gutter regions with common invisible one,
    # to create unified handle for GitGutter and other plugins
    view.add_regions(PROTECTED_REGIONS_KEY, list(flatten(gutter_regions.values())))

    # otherwise update (or create) regions
    for squiggle, regions in highlight_regions.items():
        draw_view_region(view, squiggle, regions)

    for icon, regions in gutter_regions.items():
        draw_view_region(view, icon, regions)


class GutterIcon(str):
    namespace = 'SL.Gutter'  # type: str
    scope = ''  # type: str
    icon = ''  # type: str
    flags = sublime.HIDDEN  # type: int
    linter_name = ''  # type: str

    def __new__(cls, linter_name, scope, icon):
        # type: (str, str, str) -> GutterIcon
        key = 'SL.{}.Gutter.|{}|{}'.format(linter_name, scope, icon)
        self = super().__new__(cls, key)
        self.linter_name = linter_name
        self.scope = scope
        self.icon = icon
        return self


class Squiggle(str):
    namespace = 'SL.Squiggle'  # type: str
    scope = ''  # type: str
    alt_scope = ''  # type: str
    icon = ''  # type: str
    flags = 0  # type: int
    linter_name = ''  # type: str
    uid = ''  # type: str
    demotable = False  # type: bool
    annotation = ""  # type: str

    def __new__(cls, linter_name, uid, scope, flags, demotable=False, alt_scope=None, annotation=""):
        # type: (str, str, str, int, bool, str, str) -> Squiggle
        key = (
            'SL.{}.Highlights.|{}|{}|{}'
            .format(linter_name, uid, scope, flags)
        )
        self = super().__new__(cls, key)
        self.scope = scope
        if alt_scope is None:
            self.alt_scope = scope
        else:
            self.alt_scope = alt_scope
        self.flags = flags
        self.linter_name = linter_name
        self.uid = uid
        self.demotable = demotable
        self.annotation = annotation
        return self

    def _replace(self, **overrides):
        # type: (...) -> Squiggle
        base = {
            name: overrides.pop(name, getattr(self, name))
            for name in {
                'linter_name', 'uid', 'scope', 'flags', 'demotable', 'alt_scope', 'annotation'
            }
        }
        return Squiggle(**base)

    def visible(self):
        # type: () -> bool
        return bool(self.icon or (self.scope and not self.flags == sublime.HIDDEN))

    def intentional_empty(self):
        # type: () -> bool
        return (
            self.flags & sublime.DRAW_EMPTY_AS_OVERWRITE
            == sublime.DRAW_EMPTY_AS_OVERWRITE
        )


def get_demote_scope():
    return persist.settings.get('highlights.demote_scope')


def get_demote_predicate():
    # type: () -> DemotePredicate
    setting = persist.settings.get('highlights.demote_while_editing')
    return getattr(DemotePredicates, setting, DemotePredicates.none)


class DemotePredicates:
    @staticmethod
    def none(error):
        # type: (LintError) -> bool
        return False

    @staticmethod
    def all(error):
        # type: (LintError) -> bool
        return True

    @staticmethod
    def ws_only(error):
        # type: (LintError) -> bool
        return bool(WS_ONLY.search(error['offending_text']))

    @staticmethod
    def some_ws(error):
        # type: (LintError) -> bool
        return bool(SOME_WS.search(error['offending_text']))
    ws_regions = some_ws

    @staticmethod
    def multilines(error):
        # type: (LintError) -> bool
        return bool(MULTILINES.search(error['offending_text']))

    @staticmethod
    def warnings(error):
        # type: (LintError) -> bool
        return error['error_type'] == WARNING


# --------------- ZOMBIE PROTECTION ---------------- #
#    [¬º°]¬ [¬º°]¬  [¬º˚]¬  [¬º˙]* ─ ─ ─ ─ ─ ─ ─╦╤︻ #

# Just trying and catching `NameError` reuses the previous value or
# "version" of this variable when hot-reloading
try:
    CURRENTSTORE
except NameError:
    CURRENTSTORE = defaultdict(set)  # type: Dict[sublime.ViewId, Set[RegionKey]]
try:
    EVERSTORE
except NameError:
    EVERSTORE = defaultdict(set)  # type: DefaultDict[sublime.ViewId, Set[RegionKey]]
else:
    # Assign the newly loaded classes to the old regions.
    # On each reload the `id` of our classes change and any
    # `isinstance(x, Y)` would fail.
    # Holy moly, *in-place* mutation.
    def _reload_everstore(store):
        for regions in store.values():
            for r in regions:
                if '.Highlights' in r:
                    r.__class__ = Squiggle
                elif '.Gutter' in r:
                    r.__class__ = GutterIcon

    try:
        _reload_everstore(EVERSTORE)
    except TypeError:
        # On initial migration the `EVERSTORE` only holds native strings.
        # These are not compatible, so we initialize to a fresh state.
        EVERSTORE = defaultdict(set)


@util.assert_on_ui_thread
def draw_view_region(view, key, regions):
    # type: (sublime.View, RegionKey, List[sublime.Region]) -> None
    if SUBLIME_SUPPORTS_REGION_ANNOTATIONS and isinstance(key, Squiggle):
        if key.annotation and key.visible():
            annotations = {
                "annotations": [key.annotation],
                "annotation_color":
                    view.style_for_scope(key.scope)["foreground"]
            }
        else:
            annotations = {}
        view.add_regions(
            key,
            regions,
            key.scope,
            key.icon,
            key.flags,
            **annotations
        )
    else:
        view.add_regions(key, regions, key.scope, key.icon, key.flags)
    vid = view.id()
    CURRENTSTORE[vid].add(key)
    EVERSTORE[vid].add(key)


@util.assert_on_ui_thread
def erase_view_region(view, key):
    # type: (sublime.View, RegionKey) -> None
    view.erase_regions(key)
    CURRENTSTORE[view.id()].discard(key)


def get_regions_keys(view):
    # type: (sublime.View) -> FrozenSet[RegionKey]
    return frozenset(CURRENTSTORE.get(view.id(), set()))


@util.assert_on_ui_thread
def restore_from_everstore(view):
    # type: (sublime.View) -> None
    vid = view.id()
    CURRENTSTORE[vid] = EVERSTORE[vid].copy()


class ZombieController(sublime_plugin.EventListener):
    def on_text_command(self, view, cmd, args):
        # type: (sublime.View, str, Dict) -> None
        if cmd in ['undo', 'redo_or_repeat']:
            restore_from_everstore(view)

    def on_close(self, view):
        # type: (sublime.View) -> None
        sublime.set_timeout_async(lambda: EVERSTORE.pop(view.id(), None))


# ----------------------------------------------------- #


class ViewListCleanupController(sublime_plugin.EventListener):
    def on_pre_close(self, view):
        vid = view.id()
        State['idle_views'].discard(vid)
        State['quiet_views'].discard(vid)
        State['views_without_phantoms'].discard(vid)
        State['views'].discard(vid)


class RevisitErrorRegions(sublime_plugin.EventListener):
    @util.distinct_until_buffer_changed
    def on_modified(self, view):
        if not util.is_lintable(view):
            return

        active_view = State['active_view']
        if active_view and view.buffer_id() == active_view.buffer_id():
            view = active_view

        revalidate_regions(view)
        # Run `maybe_update_error_store` on the worker because it
        # potentially wants to mutate the store. We do this always
        # on the worker queue to avoid using locks.
        sublime.set_timeout_async(lambda: maybe_update_error_store(view))


@util.ensure_on_ui_thread
def revalidate_regions(view):
    # type: (sublime.View) -> None
    vid = view.id()
    if vid in State['quiet_views']:
        return

    filename = util.canonical_filename(view)
    errors = persist.file_errors.get(filename, [])
    errors_by_uid = {e['uid']: e for e in errors}

    selections = get_current_sel(view)  # frozen sel() for this operation
    region_keys = get_regions_keys(view)
    eof = view.size()
    for key in region_keys:
        if isinstance(key, Squiggle):
            # We can have keys without any region drawn for example
            # if we loaded the `EVERSTORE`.
            region = head(view.get_regions(key))
            if region is None:
                continue

            # Draw squiggles *under* the cursor invisible because
            # we don't want the visual noise exactly where we edit
            # our code.
            # Note that this also immeditaley **hides** empty regions
            # (dangles) for example if you delete a line with a squiggle
            # on it. Removing dangles is thus a two step process. We
            # first, immediately and on the UI thread, hide them, later
            # in `maybe_update_error_store` we actually erase the region
            # and remove the error from the store.
            if any(region.contains(s) for s in selections):
                draw_squiggle_invisible(view, key, [region])
                try:
                    errors_by_uid[key.uid]['revalidate'] = True  # type: ignore[typeddict-item]
                except LookupError:
                    pass

        elif isinstance(key, GutterIcon):
            # Remove gutter icon if its region is empty,
            # e.g. the user deleted the squiggled word.
            regions = view.get_regions(key)
            filtered_regions = [
                region
                for region in regions
                if not region.empty() or (
                    # There is no 1:1 mapping from a GutterKey to an error as
                    # it is for Squiggles, so we can't have an
                    # `intentional_empty` flag either.  Thus, we do the right
                    # thing by observing:
                    # Keep the icon for an empty region, if it's at EOF
                    # position *and* the cursor is not in it. This is probably
                    # good enough for an edge case.
                    region.a == eof
                    and not any(region.contains(s) for s in selections)
                )
            ]
            if len(filtered_regions) != len(regions):
                draw_view_region(view, key, filtered_regions)


def maybe_update_error_store(view):
    # type: (sublime.View) -> None
    filename = util.canonical_filename(view)
    errors = persist.file_errors.get(filename)
    if not errors:
        return

    region_keys = get_regions_keys(view)
    uid_key_map = {
        key.uid: key
        for key in region_keys
        if isinstance(key, Squiggle)
    }

    changed = False
    new_errors = []
    regions_to_erase = []
    for error in errors:
        uid = error['uid']
        key = uid_key_map.get(uid, None)
        if key is None:
            continue

        region = head(view.get_regions(key))
        if region is None or region == error['region']:
            new_errors.append(error)
            continue

        changed = True

        if region.empty() and not key.intentional_empty():
            # Either the user edited away our region (and the error)
            # or: Dangle! Sublime has invalidated our region, it has
            # zero length (and moved to a different line at col 0).
            # It is useless now so we remove the error by not
            # copying it.
            regions_to_erase.append(key)
            continue

        line, start = view.rowcol(region.begin())
        error = error.copy()
        error.update({
            'region': region,
            'line': line,
            'start': start,
        })
        new_errors.append(error)

    if changed:
        _erase_view_regions(view, regions_to_erase)
        persist.file_errors[filename] = new_errors
        events.broadcast('updated_error_positions', {'filename': filename})


@util.ensure_on_ui_thread
def _erase_view_regions(view, keys):
    for key in keys:
        erase_view_region(view, key)


class IdleViewController(sublime_plugin.EventListener):
    def on_activated_async(self, active_view):
        previous_view = State['active_view']
        State.update({
            'active_view': active_view,
            'current_sel': get_current_sel(active_view)
        })

        if previous_view and previous_view.id() != active_view.id():
            set_idle(previous_view, True)

        set_idle(active_view, True)

    @util.distinct_until_buffer_changed
    def on_modified_async(self, view):
        active_view = State['active_view']
        if active_view and view.buffer_id() == active_view.buffer_id():
            set_idle(active_view, False)

    @util.distinct_until_buffer_changed
    def on_post_save_async(self, view):
        active_view = State['active_view']
        if active_view and view.buffer_id() == active_view.buffer_id():
            set_idle(active_view, True)

    def on_selection_modified_async(self, view):
        active_view = State['active_view']
        # Do not race between `plugin_loaded` and this event handler
        if active_view is None:
            return

        if view.buffer_id() != active_view.buffer_id():
            return

        current_sel = get_current_sel(active_view)
        if current_sel != State['current_sel']:
            State.update({'current_sel': current_sel})

            time_to_idle = persist.settings.get('highlights.time_to_idle')
            queue.debounce(
                partial(set_idle, active_view, True),
                delay=time_to_idle,
                key='highlights.{}'.format(active_view.id())
            )


def set_idle(view, idle):
    vid = view.id()

    current_idle = vid in State['idle_views']
    if idle != current_idle:
        if idle:
            State['idle_views'].add(vid)
        else:
            State['idle_views'].discard(vid)

        toggle_demoted_regions(view, idle)


@util.ensure_on_ui_thread
def toggle_demoted_regions(view, show):
    # type: (sublime.View, bool) -> None
    vid = view.id()
    if vid in State['quiet_views']:
        return

    region_keys = get_regions_keys(view)
    demote_scope = get_demote_scope()
    for key in region_keys:
        if isinstance(key, Squiggle) and key.demotable:
            regions = view.get_regions(key)
            if show:
                redraw_squiggle(view, key, regions)
            else:
                draw_squiggle_with_different_scope(view, key, regions, demote_scope)


class sublime_linter_toggle_highlights(sublime_plugin.WindowCommand):
    def run(self, what=["squiggles", "phantoms"]):
        view = self.window.active_view()
        if not view:
            return

        vid = view.id()
        if "squiggles" in what:
            hidden = vid in State['quiet_views']
            if hidden:
                State['quiet_views'].discard(vid)
            else:
                State['quiet_views'].add(vid)
            toggle_all_regions(view, show=hidden)

        if "phantoms" in what:
            if vid in State['views_without_phantoms']:
                State['views_without_phantoms'].discard(vid)
            else:
                State['views_without_phantoms'].add(vid)
            draw_phantoms(view)


HIDDEN_SCOPE = ''


@util.ensure_on_ui_thread
def toggle_all_regions(view, show):
    # type: (sublime.View, bool) -> None
    region_keys = get_regions_keys(view)
    for key in region_keys:
        if isinstance(key, Squiggle):
            regions = view.get_regions(key)
            if show:
                redraw_squiggle(view, key, regions)
            else:
                draw_squiggle_invisible(view, key, regions)


def draw_squiggle_invisible(view, key, regions):
    # type: (sublime.View, Squiggle, List[sublime.Region]) -> Squiggle
    return draw_squiggle_with_different_scope(view, key, regions, HIDDEN_SCOPE)


def draw_squiggle_with_different_scope(view, key, regions, scope):
    # type: (sublime.View, Squiggle, List[sublime.Region], str) -> Squiggle
    new_key = key._replace(scope=scope, alt_scope=key.scope)
    erase_view_region(view, key)
    draw_view_region(view, new_key, regions)
    return new_key


def redraw_squiggle(view, key, regions):
    # type: (sublime.View, Squiggle, List[sublime.Region]) -> Squiggle
    new_key = key._replace(scope=key.alt_scope)
    erase_view_region(view, key)
    draw_view_region(view, new_key, regions)
    return new_key


# --------------- UTIL FUNCTIONS ------------------- #


def get_current_sel(view):
    # type: (sublime.View) -> Tuple[sublime.Region, ...]
    return tuple(s for s in view.sel())


def head(iterable):
    # type: (Iterable[T]) -> Optional[T]
    return next(iter(iterable), None)


def all_views_into_file(filename):
    for window in sublime.windows():
        for view in window.views():
            if util.canonical_filename(view) == filename:
                yield view


# --------------- TOOLTIP HANDLING ----------------- #


class TooltipController(sublime_plugin.EventListener):
    def on_hover(self, view, point, hover_zone):
        if hover_zone == sublime.HOVER_GUTTER:
            if persist.settings.get('show_hover_line_report'):
                line_region = view.line(point)
                if any(
                    region.intersects(line_region)
                    for key in get_regions_keys(view)
                    if isinstance(key, GutterIcon)
                    for region in view.get_regions(key)
                ):
                    open_tooltip(view, point, line_report=True)

        elif hover_zone == sublime.HOVER_TEXT:
            if persist.settings.get('show_hover_region_report'):
                if any(
                    region.contains(point)
                    for key in get_regions_keys(view)
                    if isinstance(key, Squiggle) and key.visible()
                    for region in view.get_regions(key)
                ):
                    open_tooltip(view, point, line_report=False)


class SublimeLinterLineReportCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return

        point = view.sel()[0].begin()
        open_tooltip(view, point, line_report=True)


TOOLTIP_STYLES = '''
    body {
        word-wrap: break-word;
    }
    .error {
        color: var(--redish);
        font-weight: bold;
    }
    .warning {
        color: var(--yellowish);
        font-weight: bold;
    }
    .footer {
         margin-top: 0.5em;
        font-size: .92em;
        color: color(var(--background) blend(var(--foreground) 50%));
    }
    .action {
        text-decoration: none;
    }
    .icon {
        font-family: sans-serif;
        margin-top: 0.5em;
    }
'''

TOOLTIP_TEMPLATE = '''
    <body id="sublimelinter-tooltip">
        <style>{stylesheet}</style>
        <div>{content}</div>
        <div class="footer"><a href="copy">Copy</a><span>{help_text}</div>
    </body>
'''
QUICK_FIX_HELP = " | Click <span class='icon'>⌦</span> to trigger a quick action"


def get_errors_where(filename, fn):
    # type: (str, Callable[[sublime.Region], bool]) -> List[LintError]
    return [
        error for error in persist.file_errors[filename]
        if fn(error['region'])
    ]


def open_tooltip(view, point, line_report=False):
    # type: (sublime.View, int, bool) -> None
    """Show a tooltip containing all linting errors on a given line."""
    # Leave any existing popup open without replacing it
    # don't let the popup flicker / fight with other packages
    if view.is_popup_visible():
        return

    filename = util.canonical_filename(view)
    if line_report:
        line = view.full_line(point)
        errors = get_errors_where(
            filename, lambda region: region.intersects(line))
    else:
        errors = get_errors_where(
            filename, lambda region: region.contains(point))

    if not errors:
        return

    tooltip_message, quick_actions = join_msgs(errors, show_count=line_report, width=80, pt=point)

    def on_navigate(href: str) -> None:
        if href == "copy":
            sublime.set_clipboard(join_msgs_raw(errors))
            window = view.window()
            if window:
                window.status_message("SublimeLinter: info copied to clipboard")
        else:
            fixer = quick_actions[href]
            quick_fix.apply_fix(fixer, view)

        view.hide_popup()

    help_text = QUICK_FIX_HELP if quick_actions else ""
    view.show_popup(
        TOOLTIP_TEMPLATE.format(
            stylesheet=TOOLTIP_STYLES, content=tooltip_message, help_text=help_text
        ),
        flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
        location=point,
        max_width=1000,
        on_navigate=on_navigate
    )


def join_msgs_raw(errors):
    # Take an `errors` iterable and reduce it to a string without HTML tags.
    sorted_errors = sorted(errors, key=lambda e: (e["linter"], e["error_type"]))
    return "\n\n".join(
        "{}: {}\n{}{}".format(
            error["linter"],
            error["error_type"],
            error["code"] + " - " if error["code"] else "",
            error["msg"]
        ) for error in sorted_errors
    )


def join_msgs(errors, show_count, width, pt):
    # type: (List[LintError], bool, int, int) -> Tuple[str, Dict[str, quick_fix.Fix]]
    if show_count:
        part = '''
            <div class="{classname}">{count} {heading}</div>
            <div>{messages}</div>
        '''
    else:
        part = '''
            <div>{messages}</div>
        '''

    grouped_by_type = defaultdict(list)
    for error in errors:
        grouped_by_type[error["error_type"]].append(error)

    def sort_by_type(error_type):
        if error_type == WARNING:
            return "0"
        elif error_type == ERROR:
            return "1"
        else:
            return error_type

    all_msgs = ""
    quick_actions = {}  # type: Dict[str, quick_fix.Fix]
    for error_type in sorted(grouped_by_type.keys(), key=sort_by_type):
        errors_by_type = sorted(
            grouped_by_type[error_type],
            key=lambda e: (e["linter"], e["region"])
        )

        filled_templates = []
        for error in errors_by_type:
            first_line_prefix = "{linter}: ".format(**error)
            hanging_indent = len(first_line_prefix)
            first_line_indent = hanging_indent
            if error.get("code"):
                action = quick_fix.best_action_for_error(error)
                if action:
                    id = uuid.uuid4().hex
                    quick_actions[id] = action.fn
                    first_line_prefix += (
                        '<a class="action icon" href="{action_id}">⌦</a>&nbsp;'
                        '{code}&nbsp;—&nbsp;'
                        .format(action_id=id, **error)
                    )
                    first_line_indent += len(error["code"]) + 3
                else:
                    first_line_prefix += "{code} - ".format(**error)
                    first_line_indent += len(error["code"]) + 3

            lines = list(flatten(
                textwrap.wrap(
                    msg_line,
                    width=width,
                    initial_indent=(
                        " " * first_line_indent
                        if n == 0
                        else " " * hanging_indent
                    ),
                    subsequent_indent=" " * hanging_indent
                )
                for n, msg_line in enumerate(error['msg'].splitlines())
            ))
            lines[0] = lines[0].lstrip()
            lines = list(map(escape_text, lines))
            lines[0] = first_line_prefix + lines[0]

            filled_templates += lines

        heading = error_type
        count = len(errors_by_type)
        if count > 1:  # pluralize
            heading += "s"

        all_msgs += part.format(
            classname=error_type,
            count=count,
            heading=heading,
            messages='<br />'.join(filled_templates)
        )
    return all_msgs, quick_actions


def escape_text(text):
    # type: (str) -> str
    return html.escape(text, quote=False).replace(' ', '&nbsp;')
