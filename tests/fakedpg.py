"""A stand-in for Dear PyGui, so the console's own callbacks can be tested.

Dear PyGui is a native library: calling almost any of its functions without a
graphics context segfaults the interpreter, which is why the suite never creates
one. That used to mean every test patched the two or three `dpg` functions the
code path under test happened to call, and any new call in that path crashed the
run instead of failing it.

This fake implements the surface the console actually uses — an item registry
with tags, values, configuration, children and callbacks — so a test can drive a
real callback end to end and then assert on what the widgets were told. It is a
test double, not an emulator: it does not lay anything out, and it deliberately
records rather than interprets.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any


class _Id(int):
    """An item id that can also be used as a `with` block, the way DPG's are."""

    fake: Any = None

    def __enter__(self) -> _Id:
        return self

    def __exit__(self, *exc) -> bool:
        if self.fake is not None:
            self.fake._pop_container(self)
        return False


class FakeDPG:
    """Records what the console builds and what it sets."""

    def __init__(self) -> None:
        self.items: dict[Any, dict] = {}
        self.order: list[Any] = []
        self.values: dict[Any, Any] = {}
        self.clipboard = ""
        self.focused: set[str] = set()
        self.keys: set[int] = set()
        self.viewport = {"width": 1600, "height": 900, "title": ""}
        self.stopped = False
        self.running = True
        self.callback_queue: list = []
        self.scrolls: dict[Any, tuple[float, float]] = {}
        self.global_font_scale = 1.0
        self.frames = 0
        self._next_id = 1000
        self._stack: list[Any] = []
        self._last_item: Any = None
        self._aliases: dict[str, int] = {}

    # ------------------------------------------------------------- internals

    def _new_id(self, tag: Any = 0) -> _Id:
        self._next_id += 1
        item = _Id(self._next_id)
        item.fake = self
        if tag:
            self._aliases[str(tag)] = int(item)
        return item

    def _key(self, tag: Any, item: _Id) -> Any:
        return tag if tag else int(item)

    def _create(self, kind: str, tag: Any = 0, parent: Any = 0, slot: int = 1, **config) -> _Id:
        item = self._new_id(tag)
        key = self._key(tag, item)
        holder = parent or (self._stack[-1] if self._stack else None)
        record = {
            "type": kind,
            "tag": key,
            "id": int(item),
            "parent": holder,
            "children": {0: [], 1: []},
            "config": dict(config),
            "slot": slot,
        }
        self.items[key] = record
        self.items[int(item)] = record
        self.order.append(key)
        if holder is not None and holder in self.items:
            self.items[holder]["children"][slot].append(key)
        if "default_value" in config:
            self.values[key] = config["default_value"]
        self._last_item = key
        return item

    @contextmanager
    def _container(self, kind: str, tag: Any = 0, **config):
        item = self._create(kind, tag=tag, **config)
        self._stack.append(self._key(tag, item))
        try:
            yield item
        finally:
            if self._stack and self._stack[-1] == self._key(tag, item):
                self._stack.pop()

    def _pop_container(self, item: _Id) -> None:
        if self._stack:
            self._stack.pop()

    def _record(self, tag: Any) -> dict | None:
        return self.items.get(tag)

    # ------------------------------------------------------- test conveniences

    def value(self, tag: Any) -> Any:
        return self.values.get(tag)

    def config(self, tag: Any) -> dict:
        record = self._record(tag)
        return record["config"] if record else {}

    def exists(self, tag: Any) -> bool:
        return tag in self.items

    def children_of(self, tag: Any, slot: int = 1) -> list:
        record = self._record(tag)
        return list(record["children"][slot]) if record else []

    def labels(self, tag: Any, slot: int = 1) -> list[str]:
        return [str(self.config(child).get("label", "")) for child in self.children_of(tag, slot)]

    def texts(self, tag: Any) -> list[str]:
        """Every text-ish value under a container, depth first."""
        found: list[str] = []
        for child in self.children_of(tag):
            record = self._record(child)
            if record is None:
                continue
            if record["type"] in {"text", "input_text"}:
                found.append(str(self.values.get(child, record["config"].get("default_value", ""))))
            found.extend(self.texts(child))
        return found

    def invoke(self, tag: Any, app_data: Any = None) -> Any:
        """Call the callback a widget was created (or reconfigured) with."""
        record = self._record(tag)
        if record is None:
            raise KeyError(f"no such item: {tag}")
        callback = record["config"].get("callback")
        if callback is None:
            raise AssertionError(f"item {tag} has no callback")
        user_data = record["config"].get("user_data")
        return callback(record["tag"], app_data, user_data)

    def find(self, kind: str) -> list:
        """Every item of one kind, in creation order."""
        seen: list = []
        for key in self.order:
            record = self.items.get(key)
            if record is not None and record["type"] == kind and record["tag"] == key:
                seen.append(key)
        return seen

    # ------------------------------------------------------------ context/app

    def create_context(self) -> None:
        return None

    def destroy_context(self) -> None:
        return None

    def configure_app(self, **kwargs) -> None:
        self.app_config = dict(kwargs)

    def create_viewport(self, **kwargs) -> None:
        self.viewport.update(kwargs)

    def setup_dearpygui(self) -> None:
        return None

    def show_viewport(self) -> None:
        return None

    def set_primary_window(self, tag: Any, value: bool) -> None:
        self.primary_window = (tag, value)

    def set_viewport_resize_callback(self, callback) -> None:
        self.resize_callback = callback

    def is_dearpygui_running(self) -> bool:
        return self.running and not self.stopped

    def stop_dearpygui(self) -> None:
        self.stopped = True

    def render_dearpygui_frame(self) -> None:
        self.frames += 1

    def get_callback_queue(self):
        queued, self.callback_queue = self.callback_queue, []
        return queued

    def run_callbacks(self, jobs) -> None:
        for job in jobs or []:
            if job and callable(job[0]):
                job[0](*job[1:])

    def get_dearpygui_version(self) -> str:
        return "fake"

    def split_frame(self, delay: int = 0) -> None:
        return None

    def get_frame_count(self) -> int:
        return self.frames

    # ------------------------------------------------------------- containers

    def window(self, **kwargs):
        return self._container("window", **kwargs)

    def menu_bar(self, **kwargs):
        return self._container("menu_bar", **kwargs)

    def menu(self, **kwargs):
        return self._container("menu", **kwargs)

    def group(self, **kwargs):
        return self._container("group", **kwargs)

    def child_window(self, **kwargs):
        return self._container("child_window", **kwargs)

    def table(self, **kwargs):
        return self._container("table", **kwargs)

    def table_row(self, **kwargs):
        return self._container("table_row", **kwargs)

    def node_editor(self, **kwargs):
        return self._container("node_editor", **kwargs)

    def tooltip(self, parent, **kwargs):
        return self._container("tooltip", parent=parent, **kwargs)

    def theme(self, **kwargs):
        return self._container("theme", **kwargs)

    def theme_component(self, *args, **kwargs):
        return self._container("theme_component", **kwargs)

    def font_registry(self, **kwargs):
        return self._container("font_registry", **kwargs)

    def font(self, path, size, **kwargs):
        return self._container("font", path=path, size=size, **kwargs)

    def handler_registry(self, **kwargs):
        return self._container("handler_registry", **kwargs)

    def tab_bar(self, **kwargs):
        return self._container("tab_bar", **kwargs)

    def tab(self, **kwargs):
        return self._container("tab", **kwargs)

    # ------------------------------------------------------------------ items

    def add_text(self, default_value="", **kwargs):
        return self._create("text", default_value=default_value, **kwargs)

    def add_button(self, **kwargs):
        return self._create("button", **kwargs)

    def add_input_text(self, **kwargs):
        return self._create("input_text", **kwargs)

    def add_checkbox(self, **kwargs):
        return self._create("checkbox", **kwargs)

    def add_combo(self, items=(), **kwargs):
        return self._create("combo", items=list(items), **kwargs)

    def add_listbox(self, items=(), **kwargs):
        return self._create("listbox", items=list(items), **kwargs)

    def add_selectable(self, **kwargs):
        return self._create("selectable", **kwargs)

    def add_separator(self, **kwargs):
        return self._create("separator", **kwargs)

    def add_spacer(self, **kwargs):
        return self._create("spacer", **kwargs)

    def add_menu_item(self, **kwargs):
        return self._create("menu_item", **kwargs)

    def add_child_window(self, **kwargs):
        return self._create("child_window", **kwargs)

    def add_table_column(self, **kwargs):
        return self._create("table_column", **kwargs)

    def add_table_row(self, **kwargs):
        return self._create("table_row", **kwargs)

    def add_node(self, **kwargs):
        return self._create("node", **kwargs)

    def add_node_attribute(self, **kwargs):
        return self._create("node_attribute", **kwargs)

    def add_node_link(self, first, second, **kwargs):
        # Links live in slot 0, which is what makes the console's
        # links-before-nodes teardown order observable in a test.
        return self._create("node_link", slot=0, first=first, second=second, **kwargs)

    def add_progress_bar(self, **kwargs):
        return self._create("progress_bar", **kwargs)

    def add_key_press_handler(self, key=0, **kwargs):
        return self._create("key_press_handler", key=key, **kwargs)

    def add_font_range_hint(self, hint, **kwargs):
        return self._create("font_range_hint", hint=hint, **kwargs)

    def add_font_range(self, first, last, **kwargs):
        return self._create("font_range", first=first, last=last, **kwargs)

    def add_theme_color(self, target=0, value=(0, 0, 0, 255), **kwargs):
        return self._create("theme_color", target=target, value=value, **kwargs)

    def add_theme_style(self, target=0, x=1.0, y=-1.0, **kwargs):
        return self._create("theme_style", target=target, x=x, y=y, **kwargs)

    # --------------------------------------------------------------- mutation

    def does_item_exist(self, tag) -> bool:
        return tag in self.items

    def delete_item(self, tag, **kwargs) -> None:
        record = self.items.pop(tag, None)
        if record is None:
            return
        self.items.pop(record["id"], None)
        self.items.pop(record["tag"], None)
        parent = record["parent"]
        if parent in self.items:
            for slot in (0, 1):
                children = self.items[parent]["children"][slot]
                if record["tag"] in children:
                    children.remove(record["tag"])
        for slot in (0, 1):
            for child in list(record["children"][slot]):
                self.delete_item(child)
        self.values.pop(record["tag"], None)
        if record["tag"] in self.order:
            self.order.remove(record["tag"])

    def configure_item(self, tag, **kwargs) -> None:
        record = self._record(tag)
        if record is None:
            raise SystemError(f"item not found: {tag}")
        record["config"].update(kwargs)
        if "default_value" in kwargs:
            self.values[record["tag"]] = kwargs["default_value"]

    def get_item_configuration(self, tag) -> dict:
        record = self._record(tag)
        if record is None:
            raise SystemError(f"item not found: {tag}")
        config = dict(record["config"])
        config.setdefault("width", 0)
        config.setdefault("height", 0)
        config.setdefault("show", True)
        config.setdefault("enabled", True)
        return config

    def set_value(self, tag, value) -> None:
        record = self._record(tag)
        self.values[record["tag"] if record else tag] = value

    def get_value(self, tag):
        record = self._record(tag)
        return self.values.get(record["tag"] if record else tag)

    def get_item_children(self, tag, slot: int = 1):
        record = self._record(tag)
        if record is None:
            return []
        return list(record["children"][slot])

    def get_alias_id(self, alias) -> int:
        return self._aliases.get(str(alias), 0)

    def does_alias_exist(self, alias) -> bool:
        return str(alias) in self._aliases

    def bind_theme(self, theme) -> None:
        self.bound_theme = theme

    def bind_item_theme(self, item, theme) -> None:
        record = self._record(item)
        if record is not None:
            record["config"]["theme"] = theme

    def bind_font(self, font) -> None:
        self.bound_font = font

    def set_global_font_scale(self, scale) -> None:
        self.global_font_scale = scale

    def focus_item(self, tag) -> None:
        self.focused = {tag}

    def is_item_focused(self, tag) -> bool:
        return tag in self.focused

    def is_item_shown(self, tag) -> bool:
        return bool(self.config(tag).get("show", True))

    def is_key_down(self, key) -> bool:
        return key in self.keys

    def set_clipboard_text(self, text) -> None:
        self.clipboard = text

    def get_clipboard_text(self) -> str:
        return self.clipboard

    def get_viewport_client_width(self) -> int:
        return int(self.viewport["width"])

    def get_viewport_client_height(self) -> int:
        return int(self.viewport["height"])

    def get_item_rect_size(self, tag):
        return [640, 480]

    def get_item_pos(self, tag):
        return list(self.config(tag).get("pos") or [0, 0])

    def set_item_pos(self, tag, pos) -> None:
        self.configure_item(tag, pos=list(pos))

    def set_x_scroll(self, tag, value) -> None:
        x, y = self.scrolls.get(tag, (0.0, 0.0))
        self.scrolls[tag] = (float(value), y)

    def set_y_scroll(self, tag, value) -> None:
        x, y = self.scrolls.get(tag, (0.0, 0.0))
        self.scrolls[tag] = (x, float(value))

    def get_selected_nodes(self, editor):
        return []

    def clear_selected_nodes(self, editor) -> None:
        return None

    # Constants and anything else the console reaches for. Dear PyGui exposes
    # hundreds of mv* integers; a test double should not have to enumerate them.
    def __getattr__(self, name: str):
        if name.startswith("mv"):
            value = abs(hash(name)) % 100000
            self.__dict__[name] = value
            return value
        if name.startswith(("add_", "set_", "get_", "bind_", "is_", "does_", "show_", "hide_")):
            def _noop(*args, **kwargs):
                return None

            return _noop
        raise AttributeError(name)

    def last_item(self):
        return self._last_item

    def last_container(self):
        return self._stack[-1] if self._stack else None
