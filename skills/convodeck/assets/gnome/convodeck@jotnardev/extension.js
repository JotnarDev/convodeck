// ConvoDeck — GNOME Shell top-bar menu for Claude Code conversations (GNOME 45+).
// All data and actions go through the `convodeck` command (~/.local/bin/convodeck).
// Each row: title + summary (≤20 words), folder, last-used date, rename and delete.

import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Pango from 'gi://Pango';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';
import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const HOME = GLib.get_home_dir();
const CLI = `${HOME}/.local/bin/convodeck`;
const RECENT = 3;
const SUMMARIZE_EVERY = 300; // s

const STRINGS = {
    en: {
        title: 'Claude conversations', byFolder: 'By folder', home: 'Home (~)', newConv: 'New conversation',
        openWindow: 'Open ConvoDeck window', missing: 'folder missing', none: 'No conversations yet',
        error: 'Could not read conversations (is `convodeck` installed?)',
        renameTitle: 'Rename conversation', renameHint: 'Leave it empty to go back to the automatic title.',
        rename: 'Rename', cancel: 'Cancel', del: 'Delete', deleteTitle: 'Delete this conversation?',
        deleteBody: 'It is moved to the trash; you can restore it from there.',
        recentWarn: 'It was used less than 2 minutes ago; if it is still open in a terminal, close it first.',
        gone: dir => `The folder ${dir} no longer exists, so it cannot be resumed.`,
        trashFail: 'Could not move the conversation to the trash.',
        today: 'today', yesterday: 'yesterday',
        months: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
    },
    es: {
        title: 'Conversaciones de Claude', byFolder: 'Por carpeta', home: 'Inicio (~)', newConv: 'Nueva conversación',
        openWindow: 'Abrir ventana de ConvoDeck', missing: 'sin carpeta', none: 'Aún no hay conversaciones',
        error: 'No se pudieron leer las conversaciones (¿está instalado `convodeck`?)',
        renameTitle: 'Renombrar conversación', renameHint: 'Déjalo vacío para volver al título automático.',
        rename: 'Renombrar', cancel: 'Cancelar', del: 'Eliminar', deleteTitle: '¿Eliminar esta conversación?',
        deleteBody: 'Se mueve a la papelera; puedes recuperarla desde ahí.',
        recentWarn: 'Se usó hace menos de 2 minutos; si sigue abierta en una terminal, ciérrala antes.',
        gone: dir => `La carpeta ${dir} ya no existe, así que no se puede retomar.`,
        trashFail: 'No se pudo mover la conversación a la papelera.',
        today: 'hoy', yesterday: 'ayer',
        months: ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'],
    },
};

function pickLang() {
    const forced = GLib.getenv('CONVODECK_LANG');
    if (forced === 'es' || forced === 'en') return forced;
    return GLib.get_language_names().some(l => l.startsWith('es')) ? 'es' : 'en';
}
const _ = STRINGS[pickLang()];

function run(argv) {
    return new Promise(resolve => {
        try {
            const p = Gio.Subprocess.new(argv,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            p.communicate_utf8_async(null, null, (proc, res) => {
                try {
                    const [, out] = proc.communicate_utf8_finish(res);
                    resolve(proc.get_successful() ? out ?? '' : null);
                } catch {
                    resolve(null);
                }
            });
        } catch {
            resolve(null);
        }
    });
}

function spawn(argv) {
    try {
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE);
    } catch (e) {
        Main.notifyError('ConvoDeck', e.message);
    }
}

function when(ts) {
    const d = GLib.DateTime.new_from_unix_local(ts);
    const now = GLib.DateTime.new_now_local();
    const hm = d.format('%H:%M');
    const same = (a, b) => a.get_year() === b.get_year() && a.get_day_of_year() === b.get_day_of_year();
    if (same(d, now)) return `${_.today} ${hm}`;
    if (same(d, now.add_days(-1))) return `${_.yesterday} ${hm}`;
    const base = `${d.get_day_of_month()} ${_.months[d.get_month() - 1]}`;
    return d.get_year() === now.get_year() ? `${base} ${hm}` : `${base} ${d.get_year()}`;
}

// Secondary text is dimmed with opacity so it works with light and dark shell themes alike.
function dim(actor, level = 150) {
    actor.opacity = level;
    return actor;
}

function iconButton(icon, extraClass, onClick) {
    const b = new St.Button({
        style_class: `cd-btn ${extraClass}`, can_focus: true, y_align: Clutter.ActorAlign.CENTER,
        child: new St.Icon({icon_name: icon, style_class: 'cd-btn-icon'}),
    });
    dim(b.child, 140);
    b.connect('notify::hover', () => (b.child.opacity = b.hover ? 255 : 140));
    b.connect('clicked', onClick);
    return b;
}

const RenameDialog = GObject.registerClass(
class RenameDialog extends ModalDialog.ModalDialog {
    _init(c, onDone) {
        super._init({styleClass: 'cd-dialog'});
        this.contentLayout.add_child(new Dialog.MessageDialogContent({title: _.renameTitle, description: _.renameHint}));
        this._entry = new St.Entry({text: c.title, style_class: 'cd-entry', can_focus: true, x_expand: true});
        this.contentLayout.add_child(this._entry);
        this.setInitialKeyFocus(this._entry);
        this._entry.clutter_text.set_selection(0, -1);
        const ok = () => {
            onDone(this._entry.get_text());
            this.close();
        };
        this._entry.clutter_text.connect('activate', ok);
        this.setButtons([
            {label: _.cancel, action: () => this.close(), key: Clutter.KEY_Escape},
            {label: _.rename, action: ok, default: true},
        ]);
    }
});

const DeleteDialog = GObject.registerClass(
class DeleteDialog extends ModalDialog.ModalDialog {
    _init(c, onDone) {
        super._init({styleClass: 'cd-dialog'});
        const recent = Date.now() / 1000 - c.last < 120;
        this.contentLayout.add_child(new Dialog.MessageDialogContent({
            title: _.deleteTitle,
            description: `«${c.title}»\n\n${_.deleteBody}${recent ? `\n\n${_.recentWarn}` : ''}`,
        }));
        this.setButtons([
            {label: _.cancel, action: () => this.close(), key: Clutter.KEY_Escape, default: true},
            {label: _.del, action: () => {
                onDone();
                this.close();
            }},
        ]);
    }
});

const ConvItem = GObject.registerClass(
class ConvItem extends PopupMenu.PopupBaseMenuItem {
    _init(c, showProject, actions) {
        super._init({style_class: 'cd-item'});
        this._c = c;
        if (!c.exists) dim(this, 120);

        const text = new St.BoxLayout({vertical: true, x_expand: true, style_class: 'cd-text', y_align: Clutter.ActorAlign.CENTER});
        const name = new St.Label({text: c.title, style_class: 'cd-name'});
        name.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        text.add_child(name);
        if (c.summary) {
            const sum = new St.Label({text: c.summary, style_class: 'cd-summary'});
            sum.clutter_text.line_wrap = true;
            sum.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
            sum.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
            text.add_child(dim(sum, 165));
        }
        this.add_child(text);

        const side = new St.BoxLayout({vertical: true, style_class: 'cd-side', y_align: Clutter.ActorAlign.CENTER});
        side.add_child(dim(new St.Label({
            text: c.exists ? when(c.last) : _.missing, style_class: 'cd-when', x_align: Clutter.ActorAlign.END,
        })));
        if (showProject && c.project !== '~') {
            const proj = new St.Label({text: c.project, style_class: 'cd-proj', x_align: Clutter.ActorAlign.END});
            proj.clutter_text.ellipsize = Pango.EllipsizeMode.END;
            side.add_child(dim(proj, 120));
        }
        this.add_child(side);
        this.add_child(iconButton('document-edit-symbolic', '', () => actions.rename(c)));
        this.add_child(iconButton('user-trash-symbolic', 'danger', () => actions.remove(c)));
    }

    activate(event) {
        if (this._c.exists)
            spawn([CLI, 'open', this._c.id]);
        else
            Main.notify('ConvoDeck', _.gone(this._c.cwd));
        super.activate(event);
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.0, 'ConvoDeck');
        this._alive = true;
        this._lastSummarize = 0;

        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${ext.path}/icons/convodeck-symbolic.svg`),
            style_class: 'system-status-icon',
        }));

        const head = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false, style_class: 'cd-title-item'});
        head.add_child(new St.Label({text: _.title, style_class: 'cd-title', x_expand: true}));
        this._meta = dim(new St.Label({text: '', style_class: 'cd-meta'}));
        head.add_child(this._meta);
        this.menu.addMenuItem(head);

        this._recent = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._recent);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(_.byFolder));
        this._groups = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._groups);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const nueva = new PopupMenu.PopupImageMenuItem(_.newConv, 'list-add-symbolic');
        nueva.connect('activate', () => spawn([CLI, 'new']));
        this.menu.addMenuItem(nueva);
        const win = new PopupMenu.PopupImageMenuItem(_.openWindow, 'view-list-symbolic');
        win.connect('activate', () => spawn([CLI, 'ui']));
        this.menu.addMenuItem(win);

        this._actions = {
            rename: c => {
                this.menu.close();
                new RenameDialog(c, async title => {
                    await run([CLI, 'rename', c.id, title]);
                    this._refresh();
                }).open();
            },
            remove: c => {
                this.menu.close();
                new DeleteDialog(c, async () => {
                    if (await run([CLI, 'delete', c.id, '--yes']) === null)
                        Main.notifyError('ConvoDeck', _.trashFail);
                    this._refresh();
                }).open();
            },
        };

        this.menu.connect('open-state-changed', (_m, open) => {
            if (open) {
                this._refresh();
                this._summarize();
            }
        });
        this._refresh();
    }

    async _summarize() {
        const now = Date.now() / 1000;
        if (now - this._lastSummarize < SUMMARIZE_EVERY) return;
        this._lastSummarize = now;
        await run([CLI, 'summarize']);
        if (this._alive && this.menu.isOpen) this._refresh();
    }

    async _refresh() {
        const out = await run([CLI, 'json']);
        if (!this._alive) return;
        this._recent.removeAll();
        this._groups.removeAll();
        let list = null;
        try {
            list = JSON.parse(out);
        } catch {}
        if (!Array.isArray(list)) {
            this._recent.addMenuItem(new PopupMenu.PopupMenuItem(_.error, {reactive: false}));
            this._meta.text = '';
            return;
        }
        this._meta.text = `${list.length}`;
        if (!list.length) {
            this._recent.addMenuItem(new PopupMenu.PopupMenuItem(_.none, {reactive: false}));
            return;
        }
        for (const c of list.slice(0, RECENT))
            this._recent.addMenuItem(new ConvItem(c, true, this._actions));

        const groups = new Map();
        for (const c of list) {
            if (!groups.has(c.project)) groups.set(c.project, []);
            groups.get(c.project).push(c);
        }
        for (const [proj, items] of groups) {
            const sub = new PopupMenu.PopupSubMenuMenuItem(proj === '~' ? _.home : proj);
            sub.insert_child_at_index(dim(new St.Label({
                text: `${items.length}`, style_class: 'cd-count', y_align: Clutter.ActorAlign.CENTER,
            }), 130), sub.get_n_children() - 1);
            for (const c of items) sub.menu.addMenuItem(new ConvItem(c, false, this._actions));
            this._groups.addMenuItem(sub);
        }
        if (GLib.getenv('CONVODECK_TEST') === 'folder')
            this._groups._getMenuItems()[0]?.setSubmenuShown(true);
    }

    destroy() {
        this._alive = false;
        super.destroy();
    }
});

export default class ConvoDeckExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 1, 'right');

        // For screenshots/tests in a nested shell only: CONVODECK_TEST=menu|folder|rename|delete
        const test = GLib.getenv('CONVODECK_TEST');
        if (test) {
            this._testId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 6, () => {
                Main.overview.hide();
                this._testId = 0;
                if (test === 'rename' || test === 'delete') {
                    run([CLI, 'json']).then(out => {
                        const c = JSON.parse(out)[0];
                        this._indicator?._actions[test === 'rename' ? 'rename' : 'remove'](c);
                    });
                } else {
                    this._indicator?.menu.open();
                }
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    disable() {
        if (this._testId) GLib.source_remove(this._testId);
        this._testId = 0;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
