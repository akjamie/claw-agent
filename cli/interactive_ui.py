"""
Interactive UI for Claw CLI with arrow-key navigable menus.

Priority order on each platform:
  1. simple_term_menu (Unix/macOS, cli extra) — arrow keys
  2. prompt_toolkit    (cross-platform, core dep) — arrow keys
  3. Numbered input    (stdlib)                    — type a number
"""

import getpass
import sys
from typing import List, Optional


# Try simple_term_menu first (Unix/macOS only — raises NotImplementedError on Windows)
_SIMPLE_TERM_MENU_AVAILABLE = False
try:
    from simple_term_menu import TerminalMenu
    _SIMPLE_TERM_MENU_AVAILABLE = True
except (ImportError, NotImplementedError):
    pass

# prompt_toolkit is a core dependency — used for arrow-key menus on Windows
_PROMPT_TOOLKIT_AVAILABLE = False
try:
    from prompt_toolkit.shortcuts.dialogs import radiolist_dialog
    from prompt_toolkit.styles import Style as PtStyle
    _PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    pass


def is_interactive_stdin() -> bool:
    """Return True when stdin looks like a usable interactive TTY."""
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except Exception:
        return False


def prompt_text(question: str, default: Optional[str] = None) -> str:
    """Prompt for text input with optional default.
    
    Args:
        question: The question to display
        default: Optional default value if user presses Enter without typing
        
    Returns:
        The user's input or default value
    """
    if default:
        display = f"{question} [{default}]: "
    else:
        display = f"{question}: "
    
    try:
        answer = input(display).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default or ""
    if not answer and default:
        return default
    return answer


def prompt_password(question: str = "Enter API key") -> str:
    """Prompt for password/API key input (masked).
    
    Args:
        question: The question to display
        
    Returns:
        The user's masked input, or empty string on EOF/Cancel
    """
    try:
        return getpass.getpass(f"{question}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _run_simple_term_menu(title: str, choices: List[str], default_index: int) -> Optional[int]:
    if not _SIMPLE_TERM_MENU_AVAILABLE:
        return None
    try:
        print(f"\n{title}")
        menu = TerminalMenu(
            [f"  {c}" for c in choices],
            cursor_index=default_index,
            menu_cursor="-> ",
            menu_cursor_style=("fg_green", "bold"),
            menu_highlight_style=("fg_green",),
            cycle_cursor=True,
            clear_screen=False,
        )
        idx = menu.show()
        return idx
    except Exception:
        return None


def _run_prompt_toolkit_menu(title: str, choices: List[str], default_index: int) -> Optional[int]:
    if not _PROMPT_TOOLKIT_AVAILABLE:
        return None
    try:
        print()
        style = PtStyle.from_dict({
            "dialog": "bg:#1a1a2e",
            "dialog title": "bold",
            "radio-selected": "fg:#00ff00 bold",
            "radio": "fg:#666666",
        })
        values = [(i, c) for i, c in enumerate(choices)]
        result = radiolist_dialog(
            title=title,
            text="",
            values=values,
            default=default_index,
            style=style,
        ).run()
        if result is not None and result in range(len(choices)):
            return result
        if result is not None:
            return default_index
        return None
    except Exception:
        return None


def prompt_choice(
    title: str,
    choices: List[str],
    default_index: int = 0,
) -> int:
    if not choices:
        raise ValueError("choices list cannot be empty")

    if not is_interactive_stdin():
        return default_index

    # Tier 1 — simple_term_menu (Unix/macOS via cli extra)
    idx = _run_simple_term_menu(title, choices, default_index)
    if idx is not None:
        return idx

    # Tier 2 — prompt_toolkit radiolist_dialog (cross-platform, core dep)
    idx = _run_prompt_toolkit_menu(title, choices, default_index)
    if idx is not None:
        return idx

    # Tier 3 — numbered text input (stdlib)
    print(f"\n{title}")
    num_width = len(str(len(choices)))
    for i, choice in enumerate(choices):
        marker = "→" if i == default_index else " "
        print(f"  {marker} {i + 1:>{num_width}}. {choice}")

    print()
    while True:
        try:
            answer = input(f"Select (1-{len(choices)}, Enter={default_index + 1}): ").strip()
            if not answer:
                return default_index
            selected = int(answer) - 1
            if 0 <= selected < len(choices):
                return selected
            print(f"Invalid selection. Please enter a number between 1 and {len(choices)}.")
        except ValueError:
            print(f"Invalid input. Please enter a number between 1 and {len(choices)}.")
        except (EOFError, KeyboardInterrupt):
            print()
            return default_index


def prompt_choice_with_cancel(
    title: str,
    choices: List[str],
    default_index: int = 0,
    cancel_label: str = "Cancel",
) -> int:
    """Prompt user to select from a list with a Cancel option appended.
    
    Like prompt_choice but adds a Cancel entry at the end.
    Returns the index within the original choices list, or -1 for Cancel.
    
    Args:
        title: Header/title for the selection menu
        choices: List of choice strings to display
        default_index: Index of default choice (0-based)
        cancel_label: Label for the cancel option
    
    Returns:
        Selected index from original choices, or -1 if Cancel was selected
    """
    extended = list(choices) + [cancel_label]
    idx = prompt_choice(title, extended, default_index)
    if idx >= len(choices):
        return -1
    return idx


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt for a yes/no confirmation.
    
    Args:
        question: The question to ask
        default: Default value if user just presses Enter
        
    Returns:
        True for yes, False for no
    """
    default_str = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{default_str}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    
    if not answer:
        return default
    
    return answer in ("y", "yes")


def print_header(text: str):
    """Print a section header."""
    print()
    print("◆ " + text)
    print("-" * 60)


def print_success(text: str):
    """Print a success message."""
    print(f"✓ {text}")


def print_error(text: str):
    """Print an error message."""
    print(f"✗ {text}")


def print_info(text: str):
    """Print an info message."""
    print(f"ℹ {text}")


def print_warning(text: str):
    """Print a warning message."""
    print(f"⚠ {text}")
