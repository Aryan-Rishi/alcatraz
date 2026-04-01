"""UI helpers for the Alcatraz Setup Wizard — banner, step headers, menus."""

import os
import sys

from shared import (
    console, VERSION, _dbg,
    Panel, Text, Rule, Align, box,
    PTApp, PTKeyBindings, PTLayout, PTWindow, FormattedTextControl, PTStyle,
)


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def show_banner():
    banner_text = """
 █████╗ ██╗      ██████╗ █████╗ ████████╗██████╗  █████╗ ███████╗
██╔══██╗██║     ██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗╚══███╔╝
███████║██║     ██║     ███████║   ██║   ██████╔╝███████║  ███╔╝
██╔══██║██║     ██║     ██╔══██║   ██║   ██╔══██╗██╔══██║ ███╔╝
██║  ██║███████╗╚██████╗██║  ██║   ██║   ██║  ██║██║  ██║███████╗
╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝
    """
    console.print(Panel(
        Align.center(Text(banner_text, style="bold cyan") +
                     Text("\nAlcatraz Setup Wizard", style="bold white") +
                     Text(f"  v{VERSION}\n", style="dim")),
        border_style="cyan",
        box=box.DOUBLE,
        padding=(0, 2),
    ))


def show_step_header(step_num: int, total: int, title: str, subtitle: str = ""):
    bar_filled = "\u2588" * step_num
    bar_empty = "\u2591" * (total - step_num)
    progress_bar = f"[cyan]{bar_filled}[/][dim]{bar_empty}[/]"
    pct = int((step_num / total) * 100)

    console.print()
    console.print(Rule(style="dim cyan"))
    console.print(
        f"  [bold cyan]Step {step_num}[/] of {total}  {progress_bar}  [dim]{pct}%[/]"
    )
    console.print(f"  [bold white]{title}[/]")
    if subtitle:
        console.print(f"  [dim]{subtitle}[/]")
    console.print(Rule(style="dim cyan"))
    console.print()


def show_info_box(title: str, content: str, style: str = "cyan"):
    console.print(Panel(
        content,
        title=f"[bold]{title}[/]",
        border_style=style,
        padding=(1, 2),
    ))


def show_check(label: str, passed: bool, detail: str = ""):
    icon = "[bold green]\u2713[/]" if passed else "[bold red]\u2717[/]"
    status = "[green]Found[/]" if passed else "[red]Missing[/]"
    detail_str = f" [dim]({detail})[/]" if detail else ""
    console.print(f"  {icon} {label:<30} {status}{detail_str}")


def pause():
    console.print()
    console.input("  [dim]Press Enter to continue[/]")


def step_menu(choices, initial_nav=0, continue_label="Continue", initial_row=None):
    """Unified step menu with horizontal navigation bar at the bottom.

    choices: list of (label, value) tuples for step options (e.g., edit actions).
    initial_nav: 0=Continue (default), 1=Back -- which nav button to pre-select.
    initial_row: which row to start on (0-based index into choices, or None for nav bar).

    Navigation:
      Up/Down   -- move between step options and the nav bar
      Left/Right -- toggle between Back and Continue (when on the nav bar)
      Enter  -- confirm selection

    Returns the selected option value, "back", or "next".
    """
    nav_row = len(choices)
    total_rows = nav_row + 1
    selected_row = [nav_row if initial_row is None else min(initial_row, nav_row)]
    nav_selected = [initial_nav]      # 0=Continue, 1=Back

    menu_style = PTStyle.from_dict({
        "pointer": "fg:#61afef bold",
        "highlighted": "fg:#61afef bold",
        "separator": "fg:#6c6c6c",
        "nav.active": "bold reverse",
        "nav.inactive": "fg:#6c6c6c",
        "nav.dim": "fg:#4b5263",
        "hint": "fg:#6c6c6c italic",
    })

    def get_text():
        lines = [("", "\n")]

        # Step options
        for i, (label, _value) in enumerate(choices):
            if i == selected_row[0] and selected_row[0] < nav_row:
                lines += [("class:pointer", "  \u276f "), ("class:highlighted", label)]
            else:
                lines += [("", "    "), ("", label)]
            lines.append(("", "\n"))

        # Separator
        lines.append(("class:separator", "    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"))

        # Horizontal nav bar
        on_nav = selected_row[0] == nav_row
        if on_nav:
            back_cls = "class:nav.active" if nav_selected[0] == 1 else "class:nav.inactive"
            cont_cls = "class:nav.active" if nav_selected[0] == 0 else "class:nav.inactive"
        else:
            back_cls = cont_cls = "class:nav.dim"

        lines += [
            ("class:pointer" if on_nav else "", "  \u276f " if on_nav else "    "),
            (back_cls, " \u2190 Back "),
            ("", "     "),
            (cont_cls, f" \u2192 {continue_label} "),
            ("", "\n\n"),
        ]

        # Context hint
        if on_nav:
            lines.append(("class:hint", "    \u2190 \u2192 switch   Enter confirm   \u2191 options"))
        else:
            lines.append(("class:hint", "    \u2191 \u2193 navigate   Enter select   \u2193 navigation"))
        lines.append(("", "\n"))

        return lines

    kb = PTKeyBindings()

    _dbg(f"[step_menu] choices={[c[1] for c in choices]}, nav_row={nav_row}, total_rows={total_rows}")

    @kb.add("up")
    def _up(event):
        old = selected_row[0]
        if selected_row[0] > 0:
            selected_row[0] -= 1
        _dbg(f"  [KEY] up: row {old} -> {selected_row[0]}")
        event.app.invalidate()

    @kb.add("down")
    def _down(event):
        old = selected_row[0]
        if selected_row[0] < total_rows - 1:
            selected_row[0] += 1
        _dbg(f"  [KEY] down: row {old} -> {selected_row[0]}")
        event.app.invalidate()

    @kb.add("left")
    def _left(event):
        old = nav_selected[0]
        if selected_row[0] == nav_row:
            nav_selected[0] = 1
        _dbg(f"  [KEY] left: on_nav={selected_row[0]==nav_row}, nav {old} -> {nav_selected[0]}")
        event.app.invalidate()

    @kb.add("right")
    def _right(event):
        old = nav_selected[0]
        if selected_row[0] == nav_row:
            nav_selected[0] = 0
        _dbg(f"  [KEY] right: on_nav={selected_row[0]==nav_row}, nav {old} -> {nav_selected[0]}")
        event.app.invalidate()

    @kb.add("enter")
    def _enter(event):
        if selected_row[0] < nav_row:
            val = choices[selected_row[0]][1]
            _dbg(f"  [KEY] enter: row={selected_row[0]} < nav_row={nav_row}, returning choice {val!r}")
            event.app.exit(result=val)
        else:
            val = "back" if nav_selected[0] == 1 else "next"
            _dbg(f"  [KEY] enter: row={selected_row[0]} == nav_row, nav_selected={nav_selected[0]}, returning {val!r}")
            event.app.exit(result=val)

    @kb.add("c-c")
    def _cancel(event):
        raise KeyboardInterrupt()

    height = len(choices) + 6  # options + separator + nav + hint + padding
    app = PTApp(
        layout=PTLayout(PTWindow(content=FormattedTextControl(get_text, show_cursor=False), height=height)),
        key_bindings=kb,
        style=menu_style,
        full_screen=False,
    )

    try:
        result = app.run()
        _dbg(f"[step_menu] app.run() returned: {result!r}")
        return result
    except KeyboardInterrupt:
        sys.exit(0)
