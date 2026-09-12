import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

function statePath() {
    return GLib.build_filenamev([GLib.get_user_cache_dir(), 'ai-limits', 'state.json']);
}

function fetchBin() {
    return GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'ai-limits']);
}

function tone(remaining) {
    if (remaining == null)
        return 'muted';
    if (remaining < 20)
        return 'crit';
    if (remaining < 50)
        return 'warn';
    return 'ok';
}

function fmt(remaining) {
    if (remaining == null)
        return '—';
    return String(Math.round(remaining));
}

function headlineAgy(state) {
    const rows = state.agy || [];
    const gemini = rows.filter(r => String(r.group || '').toLowerCase().includes('gemini'));
    const five = gemini.filter(r => r.window === '5h');
    if (five.length)
        return five[0].remaining;
    const pool = gemini.length ? gemini : rows;
    if (!pool.length)
        return null;
    return pool.reduce((a, b) => (a.remaining < b.remaining ? a : b)).remaining;
}

function headlineGrok(state) {
    const rows = state.grok || [];
    if (!rows.length)
        return null;
    const head = rows.find(r => r.headline) || rows[0];
    return head.remaining;
}

const LimitsButton = GObject.registerClass(
class LimitsButton extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'AI Limits', false);

        this._box = new St.BoxLayout({style_class: 'panel-status-menu-box ai-limits-box'});
        this._agy = new St.Label({
            text: 'AGY —',
            yAlign: Clutter.ActorAlign.CENTER,
            styleClass: 'ai-limits-label',
        });
        this._sep = new St.Label({
            text: ' · ',
            yAlign: Clutter.ActorAlign.CENTER,
            styleClass: 'ai-limits-sep',
        });
        this._grok = new St.Label({
            text: 'Grok —',
            yAlign: Clutter.ActorAlign.CENTER,
            styleClass: 'ai-limits-label',
        });
        this._box.add_child(this._agy);
        this._box.add_child(this._sep);
        this._box.add_child(this._grok);
        this.add_child(this._box);

        this._detail = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._detail);

        const refreshAgy = new PopupMenu.PopupMenuItem('Refresh AGY');
        refreshAgy.connect('activate', () => this._refresh(['--agy']));
        const refreshGrok = new PopupMenu.PopupMenuItem('Refresh Grok');
        refreshGrok.connect('activate', () => this._refresh(['--grok']));
        const refreshAll = new PopupMenu.PopupMenuItem('Refresh both');
        refreshAll.connect('activate', () => this._refresh([]));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(refreshAgy);
        this.menu.addMenuItem(refreshGrok);
        this.menu.addMenuItem(refreshAll);

        this._monitor = null;
        this._reload();
        this._watch();
    }

    _watch() {
        const file = Gio.File.new_for_path(statePath());
        try {
            this._monitor = file.monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._monitor.connect('changed', () => this._reload());
        } catch (e) {
            console.error(`ai-limits: monitor failed: ${e}`);
        }
    }

    _refresh(extra = []) {
        try {
            GLib.spawn_async(
                null,
                [fetchBin(), '--fetch', ...extra],
                null,
                GLib.SpawnFlags.DO_NOT_REAP_CHILD,
                null
            );
        } catch (e) {
            console.error(`ai-limits: fetch spawn failed: ${e}`);
        }
    }

    _reload() {
        const file = Gio.File.new_for_path(statePath());
        let state = {};
        try {
            const [, bytes] = file.load_contents(null);
            state = JSON.parse(new TextDecoder().decode(bytes));
        } catch {
            state = {};
        }
        const agy = headlineAgy(state);
        const grok = headlineGrok(state);
        this._agy.text = agy == null ? 'AGY —' : `AGY ${fmt(agy)}%`;
        this._grok.text = grok == null ? 'Grok —' : `Grok ${fmt(grok)}%`;
        this._applyTone(this._agy, agy);
        this._applyTone(this._grok, grok);
        this._rebuildMenu(state);
    }

    _rebuildMenu(state) {
        this._detail.removeAll();
        const addRow = (title, remaining) => {
            const item = new PopupMenu.PopupMenuItem('', {reactive: false});
            item.label.set_text(`${title}    ${fmt(remaining)}%`);
            this._detail.addMenuItem(item);
        };
        for (const row of state.agy || [])
            addRow(`${row.group} ${row.windowLabel}`, row.remaining);
        if ((state.agy || []).length && (state.grok || []).length)
            this._detail.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        for (const row of state.grok || [])
            addRow(row.label, row.remaining);
        if (state.agyError) {
            const item = new PopupMenu.PopupMenuItem(String(state.agyError), {reactive: false});
            this._detail.addMenuItem(item);
        }
        if (state.grokError) {
            const item = new PopupMenu.PopupMenuItem(String(state.grokError), {reactive: false});
            this._detail.addMenuItem(item);
        }
    }

    _applyTone(label, remaining) {
        for (const cls of ['ai-limits-ok', 'ai-limits-warn', 'ai-limits-crit', 'ai-limits-muted'])
            label.remove_style_class_name(cls);
        label.add_style_class_name(`ai-limits-${tone(remaining)}`);
    }

    _onDestroy() {
        this._monitor?.cancel();
        super._onDestroy();
    }
});

export default class AiLimitsExtension extends Extension {
    enable() {
        this._button = new LimitsButton();
        Main.panel.addToStatusArea('ai-limits', this._button, 1, 'right');
    }

    disable() {
        this._button?.destroy();
        this._button = null;
    }
}
