from qgis.PyQt.QtWidgets import (
    QAction, QAbstractItemView, QDockWidget, QStyle, QTreeView,
    QVBoxLayout, QWidget, QToolButton, QToolBar, QMenu,
)
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import (
    Qt, QSize, QPoint, QTimer, QModelIndex, QItemSelection, QItemSelectionModel,
)
from qgis.core import (
    Qgis, QgsLayerTreeLayer, QgsLayerTreeGroup, QgsMessageLog, QgsProject,
    QgsVectorLayer,
)
import os

try:
    ContextMenuPolicy = Qt.ContextMenuPolicy
    DockWidgetArea = Qt.DockWidgetArea
except AttributeError:
    ContextMenuPolicy = Qt
    DockWidgetArea = Qt

try:
    DragDrop = QAbstractItemView.DragDropMode.DragDrop
    MoveAction = Qt.DropAction.MoveAction
except AttributeError:
    DragDrop = getattr(QAbstractItemView, "DragDrop")
    MoveAction = getattr(Qt, "MoveAction")

try:
    NoFocus = Qt.FocusPolicy.NoFocus
except AttributeError:
    NoFocus = getattr(Qt, "FocusPolicy", Qt).NoFocus

SelectionFlag = getattr(QItemSelectionModel, "SelectionFlag", QItemSelectionModel)
ClearAndSelect = getattr(SelectionFlag, "ClearAndSelect")
Rows = getattr(SelectionFlag, "Rows")
NoUpdate = getattr(SelectionFlag, "NoUpdate")

SelectionMode = getattr(QAbstractItemView, "SelectionMode", QAbstractItemView)
ExtendedSelection = getattr(SelectionMode, "ExtendedSelection")

StyleState = getattr(QStyle, "StateFlag", QStyle)
StateActive = getattr(StyleState, "State_Active")


class _AlwaysActiveTreeView(QTreeView):
    """A QTreeView that always paints rows as if it had keyboard focus.

    This view deliberately never holds real keyboard focus (NoFocus policy,
    see _create_dock) — real focus has to stay on the native Layers panel
    for QGIS actions like copy/paste to work when the layer was activated
    from here. But Qt/macOS ties the selected row's colour to actual focus
    ("active" saturated blue vs. muted "inactive" grey, including native
    checkbox/branch decoration), so without this override the row would
    always render as unfocused/grey. Forcing State_Active here — where the
    view builds the style option for every part of a row, branch strip
    included — keeps the look identical to a genuinely focused native
    panel. viewOptions() is the Qt5 hook, initViewItemOption() the Qt6
    replacement; both are defined so either binding picks up the right one.
    """

    def viewOptions(self):
        option = super().viewOptions()
        option.state |= StateActive
        return option

    def initViewItemOption(self, option):
        super().initViewItemOption(option)
        option.state |= StateActive


class VisibleLayers:
    """QGIS plugin — shows a filtered view of the Layers panel containing
    only the currently visible, spatial layers.

    Architecture: a plain QTreeView shares the same QgsLayerTreeModel as the
    main Layers panel.  Invisible / non-spatial rows are hidden with
    setRowHidden(), which is per-view and does not touch the source model.
    Because the model is shared, checkbox changes propagate instantly to the
    main Layers panel and vice-versa, and every icon / legend / group feature
    comes for free.
    """

    def __init__(self, iface_):
        self.iface = iface_
        self.action = None
        self.dock = None
        self.tree_view = None       # QTreeView backed by shared QgsLayerTreeModel
        self._src_model = None      # QgsLayerTreeModel from iface.layerTreeView()
        self.button = None
        self.dock_is_open = False
        self.auto_refresh_enabled = False
        self.act_toggle_auto = None
        self._auto_timer = None
        self._action_added_to_menu = False
        self._syncing = False           # re-entrancy guard for selection sync
        self._view_sel_model = None     # selection model of tree_view
        self._native_sel_model = None   # selection model of the native Layers panel

    # ── helpers ────────────────────────────────────────────────────────────

    def _node_at(self, idx):
        """Return the QgsLayerTreeNode for *idx*, or None (e.g. legend row)."""
        if not idx.isValid() or self._src_model is None:
            return None
        try:
            return self._src_model.index2node(idx)
        except (AttributeError, RuntimeError, TypeError):
            return None

    def _group_has_visible_content(self, group_node):
        """True if *group_node* contains at least one visible, spatial layer."""
        for child in group_node.children():
            if isinstance(child, QgsLayerTreeLayer):
                if child.isVisible():
                    layer = child.layer()
                    nonspatial = isinstance(layer, QgsVectorLayer) and not layer.isSpatial()
                    if layer and not nonspatial:
                        return True
            elif isinstance(child, QgsLayerTreeGroup):
                if child.isVisible() and self._group_has_visible_content(child):
                    return True
        return False

    def _should_hide(self, node):
        """True if the row for *node* should be hidden in the VL panel."""
        if node is None:
            return False  # Legend pseudo-node — always visible
        if isinstance(node, QgsLayerTreeLayer):
            if not node.isVisible():
                return True
            layer = node.layer()
            if layer is None:
                return True
            if isinstance(layer, QgsVectorLayer) and not layer.isSpatial():
                return True
            return False
        if isinstance(node, QgsLayerTreeGroup):
            if not node.isVisible():
                return True
            return not self._group_has_visible_content(node)
        return False

    def _hide_rows(self, parent):
        """Recursively hide/show group and layer rows.

        Intentionally does NOT recurse into QgsLayerTreeLayer nodes.
        Accessing rowCount() on a layer index triggers QgsLayerTreeModel's
        lazy legend-node loading, which emits rowsInserted and causes Qt to
        auto-expand the parent node in the view — the root cause of the
        unwanted full-list expansion on every refresh.
        Legend nodes are always shown when their parent layer is visible;
        their expand/collapse state is left entirely to the user.
        """
        if self.tree_view is None or self._src_model is None:
            return
        for row in range(self._src_model.rowCount(parent)):
            idx = self._src_model.index(row, 0, parent)
            node = self._node_at(idx)
            hide = self._should_hide(node)
            self.tree_view.setRowHidden(row, parent, hide)
            if not hide and isinstance(node, QgsLayerTreeGroup):
                self._hide_rows(idx)  # Recurse into groups only

    def _refresh_hidden(self):
        """Re-apply the visibility filter.  Does NOT touch expand state so that
        drag-and-drop and auto-refresh do not collapse/expand nodes the user
        has deliberately arranged.  expandAll() is called only on first open
        and after a project load (see toggle_dock / _on_project_loaded)."""
        if self.tree_view is None or self._src_model is None:
            return
        self._hide_rows(QModelIndex())

    def _set_action_icon(self, filename, theme_fallback):
        icon_path = os.path.join(os.path.dirname(__file__), "icons", filename)
        icon = QIcon(icon_path)
        if icon.isNull():
            icon = QIcon.fromTheme(theme_fallback)
        self.action.setIcon(icon)
        if self.button:
            self.button.setIcon(icon)

    def _log_ignored_exception(self, context, exception):
        message_level = getattr(Qgis, "MessageLevel", None)
        level = getattr(message_level, "Info") if message_level else getattr(Qgis, "Info")
        QgsMessageLog.logMessage(
            f"{context}: {exception}",
            "Visible Layers",
            level,
        )

    def _native_index(self, lt_view, node):
        """Index of *node* in the native Layers view (which sits on a proxy
        model over the shared QgsLayerTreeModel), or an invalid index."""
        try:
            idx = self._src_model.node2index(node)
            model = lt_view.model()
            if model is not None and model is not self._src_model:
                idx = model.mapFromSource(idx)
            return idx
        except (AttributeError, RuntimeError, TypeError):
            return QModelIndex()

    def _push_selection_to_native(self):
        """Copy this panel's selection (layers + groups) to the native Layers
        panel, so its context menu / actions act on the whole multi-selection.
        Caller must hold the self._syncing guard."""
        lt_view = self.iface.layerTreeView()
        if lt_view is None or self.tree_view is None or self._src_model is None:
            return
        try:
            view_sm = self.tree_view.selectionModel()
            native_sm = lt_view.selectionModel()
            if view_sm is None or native_sm is None:
                return
            selection = QItemSelection()
            for idx in view_sm.selectedIndexes():
                if idx.column() != 0:
                    continue
                node = self._node_at(idx)
                if node is None:
                    continue
                native_idx = self._native_index(lt_view, node)
                if native_idx.isValid():
                    selection.select(native_idx, native_idx)
            native_sm.select(selection, ClearAndSelect | Rows)
        except (AttributeError, RuntimeError, TypeError) as exc:
            self._log_ignored_exception("Could not push selection to native panel", exc)

    def _on_view_selection_changed(self, *_):
        """Selection changed in this panel -> mirror it to the native panel."""
        if self._syncing:
            return
        self._syncing = True
        try:
            self._push_selection_to_native()
        finally:
            self._syncing = False

    def _mirror_native_selection(self, *_):
        """Native Layers panel selection / current layer changed -> mirror it
        into this panel (multi-selection included)."""
        if self._syncing or self.tree_view is None or self._src_model is None:
            return
        lt_view = self.iface.layerTreeView()
        if lt_view is None:
            return
        self._syncing = True
        try:
            view_sm = self.tree_view.selectionModel()
            if view_sm is None:
                return
            selection = QItemSelection()
            for node in lt_view.selectedNodes():
                idx = self._src_model.node2index(node)
                if idx.isValid():
                    selection.select(idx, idx)
            view_sm.select(selection, ClearAndSelect | Rows)

            # Current index only (NoUpdate keeps the multi-selection intact).
            current_idx = QModelIndex()
            layer = lt_view.currentLayer()
            if layer:
                node = self._src_model.rootGroup().findLayer(layer.id())
                if node is not None:
                    current_idx = self._src_model.node2index(node)
            view_sm.setCurrentIndex(current_idx, NoUpdate)
        except (AttributeError, RuntimeError, TypeError) as exc:
            self._log_ignored_exception("Could not mirror native selection", exc)
        finally:
            self._syncing = False

    def _connect_selection_signals(self):
        """(Re)connect both selection models. Needed again whenever a view's
        setModel() replaces its selection model (e.g. on project load)."""
        for attr, getter in (
            ("_view_sel_model", lambda: self.tree_view.selectionModel()
                if self.tree_view else None),
            ("_native_sel_model", lambda: self.iface.layerTreeView().selectionModel()
                if self.iface.layerTreeView() else None),
        ):
            old = getattr(self, attr)
            slot = (self._on_view_selection_changed if attr == "_view_sel_model"
                    else self._mirror_native_selection)
            if old is not None:
                try:
                    old.selectionChanged.disconnect(slot)
                except (RuntimeError, TypeError) as exc:
                    self._log_ignored_exception("Could not disconnect signal", exc)
            new = getter()
            if new is not None:
                new.selectionChanged.connect(slot)
            setattr(self, attr, new)

    def _disconnect_selection_signals(self):
        for attr, slot in (
            ("_view_sel_model", self._on_view_selection_changed),
            ("_native_sel_model", self._mirror_native_selection),
        ):
            sel_model = getattr(self, attr)
            if sel_model is not None:
                try:
                    sel_model.selectionChanged.disconnect(slot)
                except (RuntimeError, TypeError) as exc:
                    self._log_ignored_exception("Could not disconnect signal", exc)
            setattr(self, attr, None)

    def _sync_current_layer(self, layer, set_active=True):
        """Make a Visible Layers click behave like a native Layers panel click."""
        if not layer:
            return
        # Qt's default click handling already updated this view's own
        # current/selected index (see _create_dock for why the two views
        # can't just share one selection model).
        if set_active:
            # setActiveLayer() resets the native selection to that single
            # layer, so re-push our (possibly multi-)selection right after.
            self._syncing = True
            try:
                self.iface.setActiveLayer(layer)
                self._push_selection_to_native()
            finally:
                self._syncing = False

        # Move real keyboard focus to the native layer tree view. Confirmed
        # necessary: QGIS's copy/paste-features workflow does not work
        # reliably off activeLayer() alone — it also depends on the native
        # panel actually holding focus. tree_view has NoFocus policy (see
        # _create_dock), so there's no race with Qt handing focus back to
        # it — this can run synchronously, no flicker.
        lt_view = self.iface.layerTreeView()
        if lt_view:
            try:
                lt_view.setFocus()
            except (AttributeError, RuntimeError, TypeError) as exc:
                self._log_ignored_exception("Could not focus layer tree view", exc)

    # ── initGui / unload ──────────────────────────────────────────────────

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "glasses_on.svg")
        icon = QIcon(icon_path)
        if icon.isNull():
            icon = QIcon.fromTheme("view-visible")

        self.action = QAction(icon, "Visible Layers Panel", self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        self._inject_button_in_layer_panel_toolbar()
        self._inject_action_in_layer_panel_menu()

        root = QgsProject.instance().layerTreeRoot()
        root.visibilityChanged.connect(self._on_visibility_changed)
        QgsProject.instance().layerWasAdded.connect(self._on_layer_added)
        QgsProject.instance().layerWillBeRemoved.connect(self._on_any_change)
        QgsProject.instance().readProject.connect(self._on_project_loaded)

    def unload(self):
        root = QgsProject.instance().layerTreeRoot()
        for sig, slot in [
            (root.visibilityChanged, self._on_visibility_changed),
            (QgsProject.instance().layerWasAdded, self._on_layer_added),
            (QgsProject.instance().layerWillBeRemoved, self._on_any_change),
            (QgsProject.instance().readProject, self._on_project_loaded),
        ]:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError) as exc:
                self._log_ignored_exception("Could not disconnect signal", exc)

        if self.tree_view is not None:
            lt_view = self.iface.layerTreeView()
            if lt_view:
                try:
                    lt_view.currentLayerChanged.disconnect(self._mirror_native_selection)
                except (RuntimeError, TypeError) as exc:
                    self._log_ignored_exception("Could not disconnect signal", exc)
        self._disconnect_selection_signals()

        self._disconnect_model_signals()

        if self._auto_timer:
            self._auto_timer.stop()

        if self.action:
            lt_view = self.iface.layerTreeView()
            parent = lt_view.parent() if lt_view else None
            toolbar = parent.findChild(QToolBar) if parent else None
            if toolbar:
                toolbar.removeAction(self.action)
            self.action = None

        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock = None
        if self.button:
            self.button.setParent(None)
            self.button = None
        self.tree_view = None
        self._src_model = None

    # ── toolbar / menu injection ───────────────────────────────────────────

    def _inject_button_in_layer_panel_toolbar(self):
        lt_view = self.iface.layerTreeView()
        parent = lt_view.parent() if lt_view else None
        toolbar = parent.findChild(QToolBar) if parent else None
        if toolbar:
            toolbar.addAction(self.action)
            for widget in toolbar.findChildren(QToolButton):
                if widget.defaultAction() == self.action:
                    self.button = widget
                    break

    def _inject_action_in_layer_panel_menu(self):
        """Schedule the action to be added to the Layers Panel title-bar menu."""
        for delay in (100, 500, 1000, 2000):
            QTimer.singleShot(delay, self._add_action_to_dock_menu)

    def _add_action_to_dock_menu(self):
        if self._action_added_to_menu:
            return
        try:
            main_window = self.iface.mainWindow()
            if not main_window:
                return

            layer_tree_view = self.iface.layerTreeView()
            dock = None

            # Strategy 1: look by known object names / title
            for candidate in main_window.findChildren(QDockWidget):
                name = candidate.objectName()
                if name in ('LayersPanel', 'Layers', 'qgis_layer_tree_dock'):
                    if layer_tree_view and candidate.widget():
                        if layer_tree_view in candidate.widget().findChildren(
                                type(layer_tree_view)):
                            dock = candidate
                            break
                elif 'layer' in candidate.windowTitle().lower():
                    if layer_tree_view and candidate.widget():
                        if layer_tree_view in candidate.widget().findChildren(
                                type(layer_tree_view)):
                            dock = candidate
                            break

            # Strategy 2: walk up from the layer tree view
            if not dock and layer_tree_view:
                widget = layer_tree_view
                for _ in range(10):
                    p = widget.parent()
                    if isinstance(p, QDockWidget):
                        dock = p
                        break
                    if p is None:
                        break
                    widget = p

            if not dock:
                return

            # Find the title-bar options button
            options_button = None
            for btn in dock.findChildren(QToolButton):
                if btn.objectName() == 'qt_dockwidget_options':
                    options_button = btn
                    break
            if not options_button:
                for btn in dock.findChildren(QToolButton):
                    if btn.menu() is not None and btn is not self.button:
                        options_button = btn
                        break

            if not options_button:
                return

            menu = options_button.menu()
            if menu is None:
                menu = QMenu(options_button)
                options_button.setMenu(menu)

            if self.action not in menu.actions():
                if menu.actions():
                    menu.addSeparator()
                menu.addAction(self.action)
                self._action_added_to_menu = True

        except (AttributeError, RuntimeError, TypeError) as exc:
            self._log_ignored_exception("Could not add action to dock menu", exc)

    # ── dock creation / toggle ─────────────────────────────────────────────

    def toggle_dock(self):
        if not self.dock:
            self._create_dock()
        if self.dock_is_open:
            self.dock.hide()
            self._set_action_icon("glasses_on.svg", "view-visible")
            self.dock_is_open = False
        else:
            self._refresh_hidden()
            self.dock.show()
            self._set_action_icon("glasses_off.svg", "view-hidden")
            self.dock_is_open = True

    def _create_dock(self):
        # ── Grab the shared model from the main Layers panel ──────────────
        lt_view = self.iface.layerTreeView()
        self._src_model = lt_view.layerTreeModel()

        # ── QTreeView backed by the shared model ──────────────────────────
        self.tree_view = _AlwaysActiveTreeView()
        self.tree_view.setModel(self._src_model)

        # Never let this view take real keyboard focus — it's always kept
        # on the native Layers panel (see _sync_current_layer) so QGIS
        # actions like copy/paste work. Selection/checkbox clicks work
        # fine without focus.
        self.tree_view.setFocusPolicy(NoFocus)

        # Note: an earlier attempt just reused lt_view.selectionModel() here,
        # assuming a shared selection model would keep both views in sync
        # for free. In practice nothing got synced — most likely because
        # lt_view's own view-level model (lt_view.model(), used for its
        # search/filter box) is a proxy over this layerTreeModel(), so its
        # selection model is bound to proxy indexes rather than the raw
        # model indexes tree_view uses; Qt silently ignores a selection
        # model that targets a different model. Sync explicitly instead,
        # in both directions:
        # Visible Layers click -> native panel: _sync_current_layer()
        # Native panel click -> Visible Layers: _mirror_native_selection()
        # Multi-selection (Ctrl/Cmd/Shift-click) is mirrored the same way,
        # through both views' selectionChanged signals.
        self.tree_view.setSelectionMode(ExtendedSelection)
        lt_view.currentLayerChanged.connect(self._mirror_native_selection)
        self._connect_selection_signals()

        self.tree_view.header().setVisible(False)
        self.tree_view.setIndentation(14)
        self.tree_view.setContextMenuPolicy(ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)
        self.tree_view.doubleClicked.connect(self._on_double_clicked)
        self.tree_view.clicked.connect(self._on_clicked)

        # Drag-and-drop reordering — the shared QgsLayerTreeModel handles the
        # actual move, so reordering here propagates to the Layers panel.
        self.tree_view.setDragEnabled(True)
        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDropIndicatorShown(True)
        self.tree_view.setDragDropMode(DragDrop)
        self.tree_view.setDefaultDropAction(MoveAction)

        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setStyleSheet("QToolBar { border: none; }")

        refresh_action = QAction(
            QIcon(":/images/themes/default/mActionRefresh.svg"),
            "Refresh", self.iface.mainWindow(),
        )
        refresh_action.triggered.connect(self._refresh_hidden)
        toolbar.addAction(refresh_action)

        icon_off_path = os.path.join(os.path.dirname(__file__), "icons", "mActionOff.svg")
        icon_off = QIcon(icon_off_path)
        if icon_off.isNull():
            icon_off = QIcon.fromTheme("media-playback-stop")
        self.act_toggle_auto = QAction(self.iface.mainWindow())
        self.act_toggle_auto.setIcon(icon_off)
        self.act_toggle_auto.setToolTip("Activate Auto-refresh")
        self.act_toggle_auto.setCheckable(True)
        self.act_toggle_auto.setChecked(False)
        self.act_toggle_auto.triggered.connect(self._toggle_auto_refresh)
        toolbar.addAction(self.act_toggle_auto)

        expand_action = QAction(
            QIcon(":/images/themes/default/mActionExpandTree.svg"),
            "Expand All", self.iface.mainWindow(),
        )
        expand_action.triggered.connect(self.tree_view.expandAll)
        toolbar.addAction(expand_action)

        collapse_action = QAction(
            QIcon(":/images/themes/default/mActionCollapseTree.svg"),
            "Collapse All", self.iface.mainWindow(),
        )
        collapse_action.triggered.connect(self.tree_view.collapseAll)
        toolbar.addAction(collapse_action)

        # ── Layout ────────────────────────────────────────────────────────
        main_widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.tree_view)
        main_widget.setLayout(layout)

        # ── Dock widget ───────────────────────────────────────────────────
        self.dock = QDockWidget("Visible Layers", self.iface.mainWindow())
        self.dock.setWidget(main_widget)
        self.iface.addDockWidget(DockWidgetArea.LeftDockWidgetArea, self.dock)
        self.dock.visibilityChanged.connect(self._update_dock_state)

        if self.auto_refresh_enabled:
            self._connect_model_signals()

    def _update_dock_state(self, visible):
        self.dock_is_open = visible
        if not visible:
            self._set_action_icon("glasses_on.svg", "view-visible")

    # ── signal handlers ───────────────────────────────────────────────────

    def _on_visibility_changed(self, node):
        if self.dock_is_open and self.auto_refresh_enabled:
            self._schedule_refresh()

    def _on_layer_added(self, *_):
        """Always-on: refresh when a new layer is added to the project.

        Intentionally ignores auto_refresh_enabled — a newly added layer
        should appear in the panel immediately regardless of mode.
        Uses a 100 ms debounce to let QGIS finish adding the layer fully.
        """
        if not self.dock_is_open:
            return
        self._schedule_refresh(delay_ms=100)

    def _on_any_change(self, *_):
        if self.dock_is_open and self.auto_refresh_enabled:
            self._schedule_refresh()

    def _on_model_changed(self, *_):
        if self.dock_is_open and self.auto_refresh_enabled:
            self._schedule_refresh()

    def _on_project_loaded(self):
        if not self.dock_is_open or self.tree_view is None:
            return
        lt_view = self.iface.layerTreeView()
        new_model = lt_view.layerTreeModel()
        if new_model is not self._src_model:
            self._disconnect_model_signals()   # disconnects both always-on and auto-refresh
            self._src_model = new_model
            self.tree_view.setModel(self._src_model)
            self._connect_selection_signals()   # setModel() made a new selection model
            if self.auto_refresh_enabled:
                self._connect_model_signals()
        self._refresh_hidden()

    def _schedule_refresh(self, delay_ms=60):
        """Debounce rapid-fire changes before calling _refresh_hidden."""
        if self._auto_timer is None:
            self._auto_timer = QTimer(self.iface.mainWindow())
            self._auto_timer.setSingleShot(True)
            self._auto_timer.timeout.connect(self._refresh_hidden)
        self._auto_timer.start(delay_ms)

    # ── model signal connections (used for auto-refresh) ──────────────────

    def _model_signals(self):
        """Signals used only in auto-refresh mode."""
        if self._src_model is None:
            return []
        return [
            self._src_model.rowsRemoved,
            self._src_model.layoutChanged,
            self._src_model.modelReset,
        ]

    def _connect_model_signals(self):
        for sig in self._model_signals():
            try:
                sig.disconnect(self._on_model_changed)
            except (RuntimeError, TypeError) as exc:
                self._log_ignored_exception("Could not disconnect signal", exc)
            sig.connect(self._on_model_changed)

    def _disconnect_model_signals(self):
        for sig in self._model_signals():
            try:
                sig.disconnect(self._on_model_changed)
            except (RuntimeError, TypeError) as exc:
                self._log_ignored_exception("Could not disconnect signal", exc)

    # ── auto-refresh toggle ───────────────────────────────────────────────

    def _toggle_auto_refresh(self, checked):
        self.auto_refresh_enabled = bool(checked)

        if self.auto_refresh_enabled:
            icon_name, theme, tooltip = (
                "mActionOn.svg", "media-playback-start", "Deactivate Auto-refresh")
            self._connect_model_signals()
        else:
            icon_name, theme, tooltip = (
                "mActionOff.svg", "media-playback-stop", "Activate Auto-refresh")
            self._disconnect_model_signals()

        icon = QIcon(os.path.join(os.path.dirname(__file__), "icons", icon_name))
        if icon.isNull():
            icon = QIcon.fromTheme(theme)
        self.act_toggle_auto.setIcon(icon)
        self.act_toggle_auto.setToolTip(tooltip)

        self._refresh_hidden()

    # ── interaction handlers ──────────────────────────────────────────────

    def _on_clicked(self, idx):
        node = self._node_at(idx)
        if isinstance(node, QgsLayerTreeLayer):
            # A Ctrl/Cmd-click that *deselected* this layer must not make it
            # the active layer; only refocus the native panel in that case.
            view_sm = self.tree_view.selectionModel() if self.tree_view else None
            still_selected = view_sm is None or view_sm.isSelected(idx)
            self._sync_current_layer(node.layer(), set_active=still_selected)

    def _on_double_clicked(self, idx):
        node = self._node_at(idx)
        if isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            if layer:
                self.iface.showLayerProperties(layer)

    def _show_context_menu(self, pos: QPoint):
        if self.tree_view is None:
            return
        idx = self.tree_view.indexAt(pos)
        if not idx.isValid():
            return
        node = self._node_at(idx)
        lt_view = self.iface.layerTreeView()

        if isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            if not layer:
                return
            self._sync_current_layer(layer)
            if lt_view:
                provider = lt_view.menuProvider()
                if provider:
                    menu = provider.createContextMenu()
                    if menu:
                        menu.exec(self.tree_view.mapToGlobal(pos))
                        return
            # Fallback minimal menu
            menu = QMenu(self.tree_view)
            for act in (self.iface.actionZoomToLayer(),
                        self.iface.actionRenameLayer()):
                if act:
                    menu.addAction(act)
            menu.exec(self.tree_view.mapToGlobal(pos))

        elif isinstance(node, QgsLayerTreeGroup):
            if lt_view:
                # Make the right-clicked group the native current node (the
                # selection itself was already mirrored by selectionChanged).
                native_idx = self._native_index(lt_view, node)
                native_sm = lt_view.selectionModel()
                if native_sm is not None and native_idx.isValid():
                    native_sm.setCurrentIndex(native_idx, NoUpdate)
                provider = lt_view.menuProvider()
                if provider:
                    menu = provider.createContextMenu()
                    if menu:
                        menu.exec(self.tree_view.mapToGlobal(pos))
                        return
            menu = QMenu(self.tree_view)
            act_expand = QAction("Expand all", menu)
            act_expand.triggered.connect(self.tree_view.expandAll)
            act_collapse = QAction("Collapse all", menu)
            act_collapse.triggered.connect(self.tree_view.collapseAll)
            menu.addAction(act_expand)
            menu.addAction(act_collapse)
            menu.exec(self.tree_view.mapToGlobal(pos))
