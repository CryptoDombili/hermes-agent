"""Slash command definitions and autocomplete for the Hermes CLI.

Central registry for all slash commands. Every consumer -- CLI help, gateway
dispatch, Telegram BotCommands, Slack subcommand mapping, autocomplete --
derives its data from ``COMMAND_REGISTRY``.

To add a command: add a ``CommandDef`` entry to ``COMMAND_REGISTRY``.
To add an alias: set ``aliases=("short",)`` on the existing ``CommandDef``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils import is_truthy_value
from hermes_constants import INDICATOR_STYLES

logger = logging.getLogger(__name__)

# prompt_toolkit is an optional CLI dependency â€” only needed for
# SlashCommandCompleter and SlashCommandAutoSuggest.  Gateway and test
# environments that lack it must still be able to import this module
# for resolve_command, gateway_help_lines, and COMMAND_REGISTRY.
try:
    from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
    from prompt_toolkit.completion import Completer, Completion
except ImportError:  # pragma: no cover
    AutoSuggest = object  # type: ignore[assignment,misc]
    Completer = object    # type: ignore[assignment,misc]
    Suggestion = None     # type: ignore[assignment]
    Completion = None     # type: ignore[assignment]


# ---------------------------------------------------------------------------
# CommandDef dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandDef:
    """Definition of a single slash command."""

    name: str                          # canonical name without slash: "background"
    description: str                   # human-readable description
    category: str                      # "Session", "Configuration", etc.
    aliases: tuple[str, ...] = ()      # alternative names: ("bg",)
    args_hint: str = ""                # argument placeholder: "<prompt>", "[name]"
    subcommands: tuple[str, ...] = ()  # tab-completable subcommands
    cli_only: bool = False             # only available in CLI
    gateway_only: bool = False         # only available in gateway/messaging
    gateway_config_gate: str | None = None  # config dotpath; when truthy, overrides cli_only for gateway
    # Mid-run (agent busy) gateway behavior.  Drives the Guard-2 dispatcher
    # in gateway/run.py (_dispatch_busy_slash_command) instead of a
    # hand-written per-command if-chain.  Values:
    #   "dispatch"                â€” run the command while the agent is busy
    #                               (via its normal handler, or the mid-run
    #                               variant named by ``busy_handler``).
    #   "reject"                  â€” refuse mid-run.  Without ``busy_handler``
    #                               the generic "Agent is running â€” `/<cmd>`
    #                               can't run mid-turn" catch-all is returned;
    #                               with ``busy_handler`` a command-specific
    #                               reject message is used.
    #   "interrupt_then_dispatch" â€” interrupt/kill the running agent first,
    #                               then dispatch (the /stop, /new, /reset
    #                               class).  Guard 1 (platforms/base.py)
    #                               routes these through the cancel-handoff
    #                               path via is_interrupt_then_dispatch().
    busy_policy: str = "reject"
    # Optional key of a special mid-run handler in the Guard-2 handler table
    # (gateway/run.py) for commands whose busy behavior differs from their
    # normal handler (e.g. /goal's control-verb whitelist, /queue's FIFO
    # enqueue, /model's custom busy-reject text).
    busy_handler: str | None = None
    # Registry-owned shared execution (thin slice, informational commands).
    # Names a key in ``hermes_cli.slash_exec.EXECUTORS`` â€” a pure formatter
    # producing the canonical, surface-independent core text.  Surfaces
    # resolve it via ``hermes_cli.slash_exec.run_execute`` and apply only
    # their own decoration (Rich markup, emoji/markdown, telegramize).  A
    # string key (not a callable) keeps this module import-light: the
    # gateway can import commands.py without prompt_toolkit and without
    # pulling in executor dependencies.
    execute: str | None = None


# Valid values for CommandDef.busy_policy (see field docs above).
VALID_BUSY_POLICIES: frozenset[str] = frozenset(
    {"dispatch", "reject", "interrupt_then_dispatch"}
)


# ---------------------------------------------------------------------------
# Central registry -- single source of truth
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: list[CommandDef] = [
    # Session
    CommandDef("start", "Acknowledge platform start pings without a reply", "Session",
               gateway_only=True, busy_policy="dispatch", busy_handler="start"),
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session",
               aliases=("reset",), args_hint="[name]",
               busy_policy="interrupt_then_dispatch", busy_handler="new"),
    CommandDef("topic", "Enable or inspect Telegram DM topic sessions", "Session",
               gateway_only=True, args_hint="[off|help|session-id]"),
    CommandDef("clear", "Clear screen and start a new session", "Session",
               cli_only=True),
    CommandDef("redraw", "Force a full UI repaint (recovers from terminal drift)", "Session",
               cli_only=True),
    CommandDef("history", "Show conversation history", "Session",
               cli_only=True),
    CommandDef("save", "Save the current conversation", "Session",
               cli_only=True),
    CommandDef("retry", "Retry the last message (resend to agent)", "Session"),
    CommandDef("prompt", "Compose your next prompt in $EDITOR (markdown), then send it", "Session",
               cli_only=True, args_hint="[initial text]", aliases=("compose",)),
    CommandDef("undo", "Back up N user turns and re-prompt (default 1)", "Session",
               args_hint="[N]"),
    CommandDef("title", "Set a title for the current session", "Session",
               args_hint="[name]"),
    CommandDef("handoff", "Hand off this session to a messaging platform (Telegram, Discord, etc.)", "Session",
               args_hint="<platform>", cli_only=True),
    CommandDef("branch", "Branch the current session (explore a different path)", "Session",
               aliases=("fork",), args_hint="[name]"),
    CommandDef("compress", "Compress conversation context (add 'here [N]' to keep recent N turns; --preview shows what would happen)", "Session",
               aliases=("compact",), args_hint="[here [N] | focus topic | --preview|--dry-run]"),
    CommandDef("rollback", "List or restore filesystem checkpoints", "Session",
               args_hint="[number]"),
    CommandDef("snapshot", "Create or restore state snapshots of Hermes config/state", "Session",
               cli_only=True, aliases=("snap",), args_hint="[create|restore <id>|prune]"),
    CommandDef("export", "Export a profile (config, skills, theme) to a shareable archive", "Configuration",
               cli_only=True, args_hint="[profile] [-o output.tar.gz]"),
    CommandDef("import", "Import a shared profile archive as a new profile", "Configuration",
               cli_only=True, args_hint="<archive.tar.gz> [--name <name>]"),
    CommandDef("stop", "Kill all running background processes", "Session",
               busy_policy="interrupt_then_dispatch", busy_handler="stop"),
    CommandDef("pause", "Pause new work globally (emergency stop); '/pause off' resumes", "Session",
               gateway_only=True, args_hint="[reason | off]",
               busy_policy="dispatch"),
    CommandDef("approve", "Approve a pending dangerous command", "Session",
               gateway_only=True, args_hint="[session|always]", busy_policy="dispatch"),
    CommandDef("deny", "Deny a pending dangerous command (optionally with a reason)", "Session",
               gateway_only=True, args_hint="[all] [reason]", busy_policy="dispatch"),
    CommandDef("background", "Run a prompt in the background", "Session",
               aliases=("bg", "btw"), args_hint="<prompt>", busy_policy="dispatch"),
    CommandDef("agents", "Show active agents and running tasks", "Session",
               aliases=("tasks",), busy_policy="dispatch"),
    CommandDef("journey", "Open the learning journey timeline",
               "Session", aliases=("learning", "memory-graph"), cli_only=True,
               args_hint="[list|delete <id>|edit <id>]",
               subcommands=("list", "delete", "edit")),
    CommandDef("queue", "Queue a prompt for the next turn (doesn't interrupt)", "Session",
               aliases=("q",), args_hint="<prompt>",
               busy_policy="dispatch", busy_handler="queue"),
    CommandDef("steer", "Inject a message after the next tool call without interrupting", "Session",
               args_hint="<prompt>", busy_policy="dispatch", busy_handler="steer"),
    CommandDef("goal", "Set a standing goal Hermes works on across turns until achieved", "Session",
               args_hint="[text | draft <text> | show | gate add <cmd> | pause | resume | clear | status | wait <pid> | unwait]",
               busy_policy="dispatch", busy_handler="goal"),
    CommandDef("heartbeat", "Set a recurring prompt that re-enters this session when idle", "Session",
               aliases=("hb",), args_hint="[every <interval> <prompt> | status | pause | resume | clear]",
               subcommands=("status", "pause", "resume", "clear"),
               busy_policy="dispatch"),
    CommandDef("refine", "Review this conversation now and save lessons to memory/skills", "Session",
               args_hint="[focus instructions]"),
    CommandDef("moa", "Run one prompt through the default Mixture of Agents preset, then restore your model", "Session",
               args_hint="<prompt>", busy_policy="reject", busy_handler="moa"),
    CommandDef("subgoal", "Add or manage extra criteria on the active goal", "Session",
               args_hint="[text | remove N | clear]", busy_policy="dispatch"),
    CommandDef("status", "Show session, model, token, and context info", "Session",
               busy_policy="dispatch"),
    CommandDef("egress", "Show Docker egress proxy status", "Session",
               args_hint="[status]", subcommands=("status",),
               busy_policy="dispatch", busy_handler="egress",
               execute="egress"),
    CommandDef("context", "Show detailed context window view with usage gauge, category breakdown, compression stats, and throughput", "Session",
               aliases=("ctx",), args_hint="[all]", subcommands=("all",),
               busy_policy="dispatch"),
    CommandDef("whoami", "Show your slash command access (admin / user)", "Info"),
    CommandDef("profile", "Show active profile name and home directory", "Info",
               busy_policy="dispatch", execute="profile"),
    CommandDef("sethome", "Set this chat as the home channel", "Session",
               gateway_only=True, aliases=("set-home",)),
    CommandDef("resume", "Resume a previously-named session", "Session",
               args_hint="[name]"),

    # Configuration
    CommandDef("sessions", "Browse and resume previous sessions", "Session"),

    # Configuration
    CommandDef("config", "Show current configuration", "Configuration",
               cli_only=True),
    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model"),
    CommandDef("codex-runtime", "Toggle codex app-server runtime for OpenAI/Codex models",
               "Configuration", aliases=("codex_runtime",),
               args_hint="[auto|codex_app_server]",
               busy_policy="reject", busy_handler="codex-runtime"),

    CommandDef("personality", "Set a predefined personality", "Configuration",
               args_hint="[name]"),
    CommandDef("statusbar", "Toggle the context/model status bar", "Configuration",
               cli_only=True, aliases=("sb",)),
    CommandDef("battery", "Toggle a color-coded battery indicator in the status bar",
               "Configuration", cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("timestamps", "Toggle [HH:MM] timestamps on messages and /history", "Configuration",
               cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status"), aliases=("ts",)),
    CommandDef("diff", "Show git changes in the working directory", "Info",
               args_hint="[staged|all|session] [--stat] [path...]",
               subcommands=("staged", "all", "session")),
    CommandDef("verbose", "Cycle tool progress display: off -> new -> all -> verbose -> log",
               "Configuration", cli_only=True,
               gateway_config_gate="display.tool_progress_command",
               busy_policy="dispatch"),
    CommandDef("focus", "Toggle focus view â€” show only your prompt and the final response",
               "Configuration", cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("footer", "Toggle gateway runtime-metadata footer on final replies",
               "Configuration", args_hint="[on|off|status]",
               subcommands=("on", "off", "status"), busy_policy="dispatch"),
    CommandDef("yolo", "Toggle YOLO mode (skip all dangerous command approvals)",
               "Configuration", busy_policy="dispatch"),
    CommandDef("approvals", "Show or set the persistent dangerous-command approval mode",
               "Configuration", args_hint="[manual|smart|off]",
               subcommands=("manual", "smart", "off")),
    CommandDef("reasoning", "Manage reasoning effort and display", "Configuration",
               args_hint="[level|show|hide|full|clamp] [--global]",
               subcommands=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "show", "hide", "on", "off", "full", "clamp", "--global")),
    CommandDef("fast", "Toggle fast mode â€” OpenAI Priority Processing / Anthropic Fast Mode (Normal/Fast)", "Configuration",
               args_hint="[normal|fast|status] [--global]",
               subcommands=("normal", "fast", "status", "on", "off", "--global")),
    CommandDef("skin", "Show or change theÛùâÚ$z{-®éÜj×"" Ğ¢5T%2Ò‚&Æ—7B"Â&F—6&ÆR"Â&Væ&ÆR"Ğ¢'G2Ò7V%÷FW‡Bç7Æ—B‚Ğ¢G&–Æ–æu÷76RÒ7V%÷FW‡BæVæG7v—F‚‚""Ğ Ğ¢27V&6öÖÖæB7FvS¢¦W&òv÷&G2G—VBÂ÷"6ö×ÆWF–ærF†Rf—'7Bv÷&BàĞ¢–bÆVâ‡'G2’ÓÒ÷"†ÆVâ‡'G2’ÓÒæBæ÷BG&–Æ–æu÷76R“ Ğ¢'F–ÂÒ7V%÷FW‡B–bæ÷BG&–Æ–æu÷76RVÇ6R" Ğ¢f÷"7V"–â5T%3 Ğ¢–b7V"ç7F'G7v—F‚‡'F–ÂæÆ÷vW"‚’’æB7V"Ò'F–ÂæÆ÷vW"‚“ Ğ¢––VÆB6ö×ÆWF–öâ‡7V"Â7F'E÷÷6—F–öãÒÖÆVâ‡'F–Â’ÂF—7Æ“×7V"Ğ¢&WGW&àĞ Ğ¢7V&6öÖÖæBÒ'G5³ÒæÆ÷vW"‚Ğ¢–b7V&6öÖÖæBæ÷B–â‚&Væ&ÆR"Â&F—6&ÆR"“ Ğ¢&WGW&àĞ Ğ¢'F–ÂÒ""–bG&–Æ–æu÷76RVÇ6R'G5²ÓĞĞ¢'F–ÅöÆ÷vW"Ò'F–ÂæÆ÷vW"‚Ğ¢Ç&VG’Ò6WB‡'G5³¥Ò–bG&–Æ–æu÷76RVÇ6R'G5³¢ÓÒĞ Ğ¢G'“ Ğ¢g&öÒ†W&ÖW5ö6Æ’æ6öæf–r–×÷'BÆöEö6öæf–pĞ¢g&öÒ†W&ÖW5ö6Æ’çFööÇ5ö6öæf–r–×÷'B€Ğ¢4ôäd”uU$$ÄUõDôôÅ4UE2ÀĞ¢övWE÷ÆFf÷&Õ÷FööÇ2ÀĞ¢övWE÷ÇVv–å÷FööÇ6WEö¶W—2ÀĞ¢Ğ Ğ¢6öæf–rÒÆöEö6öæf–r‚Ğ¢Væ&ÆVBÒövWE÷ÆFf÷&Õ÷FööÇ2†6öæf–rÂ&6Æ’"Â–æ6ÇVFUöFVfVÇEöÖ7÷6W'fW'3ÔfÇ6RĞ Ğ¢f÷"G5ö¶W’ÂÆ&VÂÂöFW62–â4ôäd”uU$$ÄUõDôôÅ4UE3 Ğ¢–bG5ö¶W’–âÇ&VG’÷"æ÷BG5ö¶W’ç7F'G7v—F‚‡'F–ÅöÆ÷vW"“ Ğ¢6öçF–çVPĞ¢—5ööâÒG5ö¶W’–âVæ&ÆV@Ğ¢–b7V&6öÖÖæBÓÒ&Væ&ÆR"æB—5ööã Ğ¢6öçF–çVPĞ¢–b7V&6öÖÖæBÓÒ&F—6&ÆR"æBæ÷B—5ööã Ğ¢6öçF–çVPĞ¢––VÆB6ö×ÆWF–öâ€Ğ¢G5ö¶W’ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡'F–Â’ÀĞ¢F—7Æ“×G5ö¶W’ÀĞ¢F—7Æ•öÖWFÖÆ&VÂÀĞ¢Ğ Ğ¢f÷"G5ö¶W’–â6÷'FVB…övWE÷ÇVv–å÷FööÇ6WEö¶W—2‚’“ Ğ¢–bG5ö¶W’–âÇ&VG’÷"æ÷BG5ö¶W’ç7F'G7v—F‚‡'F–ÅöÆ÷vW"“ Ğ¢6öçF–çVPĞ¢—5ööâÒG5ö¶W’–âVæ&ÆV@Ğ¢–b7V&6öÖÖæBÓÒ&Væ&ÆR"æB—5ööã Ğ¢6öçF–çVPĞ¢–b7V&6öÖÖæBÓÒ&F—6&ÆR"æBæ÷B—5ööã Ğ¢6öçF–çVPĞ¢––VÆB6ö×ÆWF–öâ€Ğ¢G5ö¶W’ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡'F–Â’ÀĞ¢F—7Æ“×G5ö¶W’ÀĞ¢F—7Æ•öÖWFÒ'ÇVv–âFööÇ6WB"ÀĞ¢Ğ Ğ¢Ö7÷6W'fW'2Ò6öæf–rævWB‚&Ö7÷6W'fW'2"’÷"·ĞĞ¢–b—6–ç7Fæ6R†Ö7÷6W'fW'2ÂF–7B“ Ğ¢f÷"6W'fW"–â6÷'FVB†Ö7÷6W'fW'2“ Ğ¢&Vf—‚Òb'·6W'fW'Ó¢ Ğ¢–b&Vf—‚–âÇ&VG’÷"æ÷B&Vf—‚ç7F'G7v—F‚‡'F–ÅöÆ÷vW"“ Ğ¢6öçF–çVPĞ¢––VÆB6ö×ÆWF–öâ€Ğ¢&Vf—‚ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡'F–Â’ÀĞ¢F—7Æ“×&Vf—‚ÀĞ¢F—7Æ•öÖWFÖb$Ô56W'fW"w·6W'fW'Òr"ÀĞ¢Ğ¢W†6WBW†6WF–öã Ğ¢&WGW&àĞ Ğ¢7FF–6ÖWF†ö@Ğ¢FVbö†æFöfeö6ö×ÆWF–öç2‡7V%÷FW‡C¢7G"Â7V%öÆ÷vW#¢7G"“ Ğ¢""%––VÆBÆFf÷&Ò6ö×ÆWF–öç2f÷"ö†æFöfbàĞ Ğ¢öffW'26öææV7FVB†Væ&ÆVB²6öæf–wW&VB’vFWv’ÆFf÷&×2â&V6÷&FV@Ğ¢†öÖR6†ææVÂ—2äõB&WV—&VBFòÆ—7BÆFf÷&Ò(	B—Bw2ögFVâÆV&æVB@Ğ¢'VçF–ÖR(	B6òF†RÖWF†–çG2v†WF†W"öæR—26WB–WBâ6ö×ÆWFW2öæÇ’F†PĞ¢f—'7B&r‡F†RÆFf÷&Ò“²öæ6RöæR—26†÷6VâÂ7F÷àĞ¢"" Ğ¢'G2Ò7V%÷FW‡Bç7Æ—B‚Ğ¢G&–Æ–æu÷76RÒ7V%÷FW‡BæVæG7v—F‚‚""Ğ¢–bÆVâ‡'G2’â÷"†ÆVâ‡'G2’ÓÒæBG&–Æ–æu÷76R“ Ğ¢&WGW&àĞ¢'F–ÂÒ""–b†æ÷B'G2÷"G&–Æ–æu÷76R’VÇ6R'G5²ÓĞĞ¢'F–ÅöÆ÷vW"Ò'F–ÂæÆ÷vW"‚Ğ¢G'“ Ğ¢g&öÒvFWv’æ6öæf–r–×÷'BÆöEövFWv•ö6öæf–pĞ Ğ¢wrÒÆöEövFWv•ö6öæf–r‚Ğ¢ÆFf÷&×2ÒwrævWEö6öææV7FVE÷ÆFf÷&×2‚Ğ¢W†6WBW†6WF–öã Ğ¢&WGW&àĞ¢f÷"ÆFf÷&Ò–âÆFf÷&×3 Ğ¢æÖRÒÆFf÷&ÒçfÇVPĞ¢–bæ÷BæÖRç7F'G7v—F‚‡'F–ÅöÆ÷vW"“ Ğ¢6öçF–çVPĞ¢G'“ Ğ¢†öÖRÒwrævWEö†öÖUö6†ææVÂ‡ÆFf÷&ÒĞ¢W†6WBW†6WF–öã Ğ¢†öÖRÒæöæPĞ¢ÖWFÒb.(i"¶†öÖRææÖWÒ"–b†öÖRæBvWFGG"††öÖRÂ&æÖR"ÂæöæR’VÇ6R'6VæBF†—26W76–öâ†W&R Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢æÖRÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡'F–Â’ÀĞ¢F—7Æ“ÖæÖRÀĞ¢F—7Æ•öÖWFÖÖWFÀĞ¢Ğ Ğ¢7FF–6ÖWF†ö@Ğ¢FVb÷W'6öæÆ—G•ö6ö×ÆWF–öç2‡7V%÷FW‡C¢7G"Â7V%öÆ÷vW#¢7G"“ Ğ¢""%––VÆB6ö×ÆWF–öç2f÷"÷W'6öæÆ—G’f–†W&ÖW5ö6Æ’çW'6öæÆ—G’â"" Ğ¢G'“ Ğ¢26–ævÆR÷væW#¢'V–ÇBÖ–ç2²W6W"÷fW'&–FW2g&öÒvVçBçW'6öæÆ—F–W2àĞ¢g&öÒ6Æ’–×÷'BÆöEö6Æ•ö6öæf–pĞ¢g&öÒ†W&ÖW5ö6Æ’çW'6öæÆ—G’–×÷'B€Ğ¢f–Æ&ÆU÷W'6öæÆ—F–W2ÀĞ¢FW67&–&U÷W'6öæÆ—G’ÀĞ¢Ğ Ğ¢W'6öæÆ—F–W2Òf–Æ&ÆU÷W'6öæÆ—F–W2†ÆöEö6Æ•ö6öæf–r‚’Ğ¢–b&æöæR"ç7F'G7v—F‚‡7V%öÆ÷vW"’æB&æöæR"Ò7V%öÆ÷vW# Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢&æöæR"ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡7V%÷FW‡B’ÀĞ¢F—7Æ“Ò&æöæR"ÀĞ¢F—7Æ•öÖWFÒ&6ÆV"W'6öæÆ—G’÷fW&Æ’"ÀĞ¢Ğ¢f÷"æÖRÂ&ö×B–âW'6öæÆ—F–W2æ—FV×2‚“ Ğ¢–bæÖRç7F'G7v—F‚‡7V%öÆ÷vW"’æBæÖRÒ7V%öÆ÷vW# Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢æÖRÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡7V%÷FW‡B’ÀĞ¢F—7Æ“ÖæÖRÀĞ¢F—7Æ•öÖWFÖFW67&–&U÷W'6öæÆ—G’‡&ö×B’ÀĞ¢Ğ¢W†6WBW†6WF–öã Ğ¢70Ğ Ğ¢FVbvWEö6ö×ÆWF–öç2‡6VÆbÂFö7VÖVçBÂ6ö×ÆWFUöWfVçB“ Ğ¢FW‡BÒFö7VÖVçBçFW‡Eö&Vf÷&Uö7W'6÷ Ğ¢–bæ÷BFW‡Bç7F'G7v—F‚‚"ò"“ Ğ¢2G'’6öçFW‡B6ö×ÆWF–öâ„6ÆVFR6öFR×7G–ÆRĞ¢7G…÷v÷&BÒ6VÆbåöW‡G&7Eö6öçFW‡E÷v÷&B‡FW‡BĞ¢–b7G…÷v÷&B—2æ÷BæöæS Ğ¢––VÆBg&öÒ6VÆbåö6öçFW‡Eö6ö×ÆWF–öç2†7G…÷v÷&BĞ¢&WGW&àĞ¢2G'’f–ÆRF‚6ö×ÆWF–öâf÷"æöâ×6Æ6‚–çW@Ğ¢F…÷v÷&BÒ6VÆbåöW‡G&7E÷F…÷v÷&B‡FW‡BĞ¢–bF…÷v÷&B—2æ÷BæöæS Ğ¢––VÆBg&öÒ6VÆbå÷F…ö6ö×ÆWF–öç2‡F…÷v÷&BĞ¢&WGW&àĞ Ğ¢26†V6²–bvRw&R6ö×ÆWF–ær7V&6öÖÖæB†&6R6öÖÖæBÇ&VG’G—VBĞ¢'G2ÒFW‡Bç7Æ—B†Ö‡7Æ—CÓĞ¢&6Uö6ÖBÒ'G5³ÒæÆ÷vW"‚Ğ¢–bÆVâ‡'G2’â÷"†ÆVâ‡'G2’ÓÒæBFW‡BæVæG7v—F‚‚""’“ Ğ¢7V%÷FW‡BÒ'G5³Ò–bÆVâ‡'G2’âVÇ6R" Ğ¢7V%öÆ÷vW"Ò7V%÷FW‡BæÆ÷vW"‚Ğ Ğ¢27F6¶VB6Æ6‚×6¶–ÆÂ–çfö6F–öç3¢gFW"÷6¶–ÆÂÖF†RW6W"ÖĞ¢26†–âÖ÷&R6¶–ÆÇ2†÷6¶–ÆÂÖ÷6¶–ÆÂÖ"(
f’Â6ò¶VWöffW&–æpĞ¢26¶–ÆÂÖ6öÖÖæB6ö×ÆWF–öç2v†–ÆRF†RÆVF–ær×6¶–ÆÂ6†–â—0Ğ¢2Væ'&ö¶Vâ‡6VR7Æ—E÷7F6¶VE÷6¶–ÆÅö6öÖÖæG2–àĞ¢2vVçB÷6¶–ÆÅö6öÖÖæG2ç’’àĞ¢–b6VÆbåö—5÷6¶–ÆÅö6öÖÖæB†&6Uö6ÖB“ Ğ¢––VÆBg&öÒ6VÆbå÷7F6¶VE÷6¶–ÆÅö6ö×ÆWF–öç2‡FW‡BĞ¢&WGW&àĞ Ğ¢2G–æÖ–26ö×ÆWF–öç2f÷"6öÖÖæG2v—F‚'VçF–ÖRÆ—7G0Ğ¢–b""æ÷B–â7V%÷FW‡C Ğ¢–b&6Uö6ÖBÓÒ"÷6¶–â# Ğ¢––VÆBg&öÒ6VÆbå÷6¶–åö6ö×ÆWF–öç2‡7V%÷FW‡BÂ7V%öÆ÷vW"Ğ¢&WGW&àĞ¢–b&6Uö6ÖBÓÒ"÷W'6öæÆ—G’# Ğ¢––VÆBg&öÒ6VÆbå÷W'6öæÆ—G•ö6ö×ÆWF–öç2‡7V%÷FW‡BÂ7V%öÆ÷vW"Ğ¢&WGW&àĞ Ğ¢2÷FööÇ2æVVG2×VÇF’×v÷&B6ö×ÆWF–öâ‡7V&6öÖÖæB²FööÇ6WBæÖRĞ¢26ò—B†æFÆW2&÷F‚7FvW2—G6VÆbÂ'—76–ærF†R6–ævÆR×v÷&@Ğ¢25T$4ôÔÔäE2'&æ6‚&VÆ÷ràĞ¢–b&6Uö6ÖBÓÒ"÷FööÇ2# Ğ¢––VÆBg&öÒ6VÆbå÷FööÇ5ö6ö×ÆWF–öç2‡7V%÷FW‡BÂ7V%öÆ÷vW"Ğ¢&WGW&àĞ Ğ¢–b&6Uö6ÖBÓÒ"ö†æFöfb# Ğ¢––VÆBg&öÒ6VÆbåö†æFöfeö6ö×ÆWF–öç2‡7V%÷FW‡BÂ7V%öÆ÷vW"Ğ¢&WGW&àĞ Ğ¢27FF–27V&6öÖÖæB6ö×ÆWF–öç0Ğ¢–b""æ÷B–â7V%÷FW‡BæB&6Uö6ÖB–â5T$4ôÔÔäE2æB6VÆbåö6öÖÖæEöÆÆ÷vVB†&6Uö6ÖB“ Ğ¢f÷"7V"–â5T$4ôÔÔäE5¶&6Uö6ÖEÓ Ğ¢–b7V"ç7F'G7v—F‚‡7V%öÆ÷vW"’æB7V"Ò7V%öÆ÷vW# Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢7V"ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡7V%÷FW‡B’ÀĞ¢F—7Æ“×7V"ÀĞ¢Ğ¢&WGW&àĞ Ğ¢v÷&BÒFW‡E³¥ĞĞ Ğ¢f÷"6ÖBÂFW62–â4ôÔÔäE2æ—FV×2‚“ Ğ¢–bæ÷B6VÆbåö6öÖÖæEöÆÆ÷vVB†6ÖB“ Ğ¢6öçF–çVPĞ¢6ÖEöæÖRÒ6ÖE³¥ĞĞ¢–b6ÖEöæÖRç7F'G7v—F‚‡v÷&B“ Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢6VÆbåö6ö×ÆWF–öå÷FW‡B†6ÖEöæÖRÂv÷&B’ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡v÷&B’ÀĞ¢F—7Æ“Ö6ÖBÀĞ¢F—7Æ•öÖWFÖFW62ÀĞ¢Ğ Ğ¢f÷"6ÖBÂ–æfò–â6VÆbåö—FW%÷6¶–ÆÅö'VæFÆW2‚’æ—FV×2‚“ Ğ¢6ÖEöæÖRÒ6ÖE³¥ĞĞ¢–b6ÖEöæÖRç7F'G7v—F‚‡v÷&B“ Ğ¢FW67&—F–öâÒ7G"†–æfòævWB‚&FW67&—F–öâ"Â%6¶–ÆÂ'VæFÆR"’Ğ¢6†÷'EöFW62ÒFW67&—F–öå³£SÒ²‚"âââ"–bÆVâ†FW67&—F–öâ’âSVÇ6R""Ğ¢6¶–ÆÅö6÷VçBÒÆVâ†–æfòævWB‚'6¶–ÆÇ2"ÂµÒ’Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢6VÆbåö6ö×ÆWF–öå÷FW‡B†6ÖEöæÖRÂv÷&B’ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡v÷&B’ÀĞ¢F—7Æ“Ö6ÖBÀĞ¢F—7Æ•öÖWFÖb.)j2·6†÷'EöFW67Ò‡·6¶–ÆÅö6÷VçGÒ6¶–ÆÇ2’"ÀĞ¢Ğ Ğ¢f÷"6ÖBÂ–æfò–â6VÆbåö—FW%÷6¶–ÆÅö6öÖÖæG2‚’æ—FV×2‚“ Ğ¢6ÖEöæÖRÒ6ÖE³¥ĞĞ¢–b6ÖEöæÖRç7F'G7v—F‚‡v÷&B“ Ğ¢FW67&—F–öâÒ7G"†–æfòævWB‚&FW67&—F–öâ"Â%6¶–ÆÂ6öÖÖæB"’Ğ¢6†÷'EöFW62ÒFW67&—F–öå³£SÒ²‚"âââ"–bÆVâ†FW67&—F–öâ’âSVÇ6R""Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢6VÆbåö6ö×ÆWF–öå÷FW‡B†6ÖEöæÖRÂv÷&B’ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡v÷&B’ÀĞ¢F—7Æ“Ö6ÖBÀĞ¢F—7Æ•öÖWFÖb.)ª·6†÷'EöFW67Ò"ÀĞ¢Ğ Ğ¢2ÇVv–â×&Vv—7FW&VB6Æ6‚6öÖÖæG0Ğ¢G'“ Ğ¢g&öÒ†W&ÖW5ö6Æ’çÇVv–ç2–×÷'BvWE÷ÇVv–åö6öÖÖæG0Ğ¢f÷"6ÖEöæÖRÂ6ÖEö–æfò–âvWE÷ÇVv–åö6öÖÖæG2‚’æ—FV×2‚“ Ğ¢–b6ÖEöæÖRç7F'G7v—F‚‡v÷&B“ Ğ¢FW62Ò7G"†6ÖEö–æfòævWB‚&FW67&—F–öâ"Â%ÇVv–â6öÖÖæB"’Ğ¢6†÷'EöFW62ÒFW65³£SÒ²‚"âââ"–bÆVâ†FW62’âSVÇ6R""Ğ¢––VÆB6ö×ÆWF–öâ€Ğ¢6VÆbåö6ö×ÆWF–öå÷FW‡B†6ÖEöæÖRÂv÷&B’ÀĞ¢7F'E÷÷6—F–öãÒÖÆVâ‡v÷&B’ÀĞ¢F—7Æ“Öb"÷¶6ÖEöæÖWÒ"ÀĞ¢F—7Æ•öÖWFÖb/	ùHÂ·6†÷'EöFW67Ò"ÀĞ¢Ğ¢W†6WBW†6WF–öã Ğ¢70Ğ Ğ Ğ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞĞ¢2–æÆ–æRWFò×7VvvW7B†v†÷7BFW‡B’f÷"6Æ6‚6öÖÖæG0Ğ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞĞ Ğ¦6Æ726Æ6„6öÖÖæDWFõ7VvvW7B„WFõ7VvvW7B“ Ğ¢""$–æÆ–æRv†÷7B×FW‡B7VvvW7F–öç2f÷"6Æ6‚6öÖÖæG2æBF†V—"7V&6öÖÖæG2àĞ Ğ¢6†÷w2F†R&W7Böb6öÖÖæB÷"7V&6öÖÖæB–âF–ÒFW‡B2–÷RG—RàĞ¢fÆÇ2&6²Fò†—7F÷'’Ö&6VB7VvvW7F–öç2f÷"æöâ×6Æ6‚–çWBàĞ¢"" Ğ Ğ¢FVbõö–æ—Eõò€Ğ¢6VÆbÀĞ¢†—7F÷'•÷7VvvW7C¢WFõ7VvvW7BÂæöæRÒæöæRÀĞ¢6ö×ÆWFW#¢6Æ6„6öÖÖæD6ö×ÆWFW"ÂæöæRÒæöæRÀĞ¢’ÓâæöæS Ğ¢6VÆbåö†—7F÷'’Ò†—7F÷'•÷7VvvW7@Ğ¢6VÆbåö6ö×ÆWFW"Ò6ö×ÆWFW"2&WW6R—G2ÖöFVÂ66†PĞ Ğ¢FVbvWE÷7VvvW7F–öâ‡6VÆbÂ'VffW"ÂFö7VÖVçB“ Ğ¢FW‡BÒFö7VÖVçBçFW‡Eö&Vf÷&Uö7W'6÷ Ğ Ğ¢2öæÇ’7VvvW7Bf÷"6Æ6‚6öÖÖæG0Ğ¢–bæ÷BFW‡Bç7F'G7v—F‚‚"ò"“ Ğ¢2fÆÂ&6²Fò†—7F÷'’f÷"&VwVÆ"FW‡@Ğ¢–b6VÆbåö†—7F÷'“ Ğ¢&WGW&â6VÆbåö†—7F÷'’ævWE÷7VvvW7F–öâ†'VffW"ÂFö7VÖVçBĞ¢&WGW&âæöæPĞ Ğ¢'G2ÒFW‡Bç7Æ—B†Ö‡7Æ—CÓĞ¢&6Uö6ÖBÒ'G5³ÒæÆ÷vW"‚Ğ Ğ¢–bÆVâ‡'G2’ÓÒæBæ÷BFW‡BæVæG7v—F‚‚""“ Ğ¢27F–ÆÂG—–ærF†R6öÖÖæBæÖS¢÷WB(i"7VvvW7B&FR Ğ¢2&VfW"F†R4„õ%DU5BÖF6†–ær6öÖÖæB6ò6†÷'BÂ†–v‚Ög&WVVæ7Ğ¢26öÖÖæB¶VW2—G2v†÷7BFW‡Bv†VâÆöævW"6öÖÖæB6†&W2—G0Ğ¢2&Vf—‚†Rærâö†R(i"&Ç"f÷"ö†VÇÂæ÷B&'F&VB"f÷ Ğ¢2ö†V'F&VC²G—RöæRÖ÷&RÆWGFW"Fò7FVW"’àĞ¢v÷&BÒFW‡E³¥ÒæÆ÷vW"‚Ğ¢f÷"6ÖB–â6÷'FVB„4ôÔÔäE2Â¶W“ÖÆVâ“ Ğ¢–b6VÆbåö6ö×ÆWFW"—2æ÷BæöæRæBæ÷B6VÆbåö6ö×ÆWFW"åö6öÖÖæEöÆÆ÷vVB†6ÖB“ Ğ¢6öçF–çVPĞ¢6ÖEöæÖRÒ6ÖE³¥Ò27G&—ÆVF–ærğĞ¢–b6ÖEöæÖRç7F'G7v—F‚‡v÷&B’æB6ÖEöæÖRÒv÷&C Ğ¢&WGW&â7VvvW7F–öâ†6ÖEöæÖU¶ÆVâ‡v÷&B“¥ÒĞ¢&WGW&âæöæPĞ Ğ¢26öÖÖæB—26ö×ÆWFR(	B7VvvW7B7V&6öÖÖæG0Ğ¢7V%÷FW‡BÒ'G5³Ò–bÆVâ‡'G2’âVÇ6R" Ğ¢7V%öÆ÷vW"Ò7V%÷FW‡BæÆ÷vW"‚Ğ Ğ¢27F6¶VB6Æ6‚×6¶–ÆÂ–çfö6F–öç3¢v†–ÆRF†RÆVF–ærFö¶Vç2f÷&ÒàĞ¢2Væ'&ö¶Vâ6¶–ÆÂ6†–âæBF†RW6W"—2G—–æræ÷F†W"÷Fö¶VâÀĞ¢2v†÷7B×7VvvW7BF†R&W7BöbF†RæW‡B6¶–ÆÂæÖRâ÷F†W'v—6RfÆÀĞ¢2F‡&÷Vv‚FòF†R†—7F÷'’fÆÆ&6²f÷"–ç7G'V7F–öâFW‡BàĞ¢–b€Ğ¢6VÆbåö6ö×ÆWFW"—2æ÷BæöæPĞ¢æB6VÆbåö6ö×ÆWFW"åö—5÷6¶–ÆÅö6öÖÖæB†&6Uö6ÖBĞ¢“ Ğ¢f÷"6ö×ÆWF–öâ–â6VÆbåö6ö×ÆWFW"å÷7F6¶VE÷6¶–ÆÅö6ö×ÆWF–öç2‡FW‡B“ Ğ¢&VÖ–æFW"Ò6ö×ÆWF–öâçFW‡E²Ö6ö×ÆWF–öâç7F'E÷÷6—F–öã¥ÒÀĞ¢–b6ö×ÆWF–öâç7F'E÷÷6—F–öâVÇ6R6ö×ÆWF–öâçFW‡@Ğ¢–b&VÖ–æFW"ç7G&—‚“ Ğ¢&WGW&â7VvvW7F–öâ‡&VÖ–æFW"Ğ Ğ¢27FF–27V&6öÖÖæG0Ğ¢–b6VÆbåö6ö×ÆWFW"—2æ÷BæöæRæBæ÷B6VÆbåö6ö×ÆWFW"åö6öÖÖæEöÆÆ÷vVB†&6Uö6ÖB“ Ğ¢&WGW&âæöæPĞ¢–b&6Uö6ÖB–â5T$4ôÔÔäE2æB5T$4ôÔÔäE5¶&6Uö6ÖEÓ Ğ¢–b""æ÷B–â7V%÷FW‡C Ğ¢f÷"7V"–â5T$4ôÔÔäE5¶&6Uö6ÖEÓ Ğ¢–b7V"ç7F'G7v—F‚‡7V%öÆ÷vW"’æB7V"Ò7V%öÆ÷vW# Ğ¢&WGW&â7VvvW7F–öâ‡7V%¶ÆVâ‡7V%÷FW‡B“¥ÒĞ Ğ¢2fÆÂ&6²Fò†—7F÷'Ğ¢–b6VÆbåö†—7F÷'“ Ğ¢&WGW&â6VÆbåö†—7F÷'’ævWE÷7VvvW7F–öâ†'VffW"ÂFö7VÖVçBĞ¢&WGW&âæöæPĞ Ğ Ğ¦FVböf–ÆU÷6—¦UöÆ&VÂ‡Fƒ¢7G"’Óâ7G# Ğ¢""%&WGW&â6ö×7B‡VÖâ×&VF&ÆRf–ÆR6—¦RÂ÷"rröâW'&÷"â"" Ğ¢G'“ Ğ¢6—¦RÒ÷2çF‚ævWG6—¦R‡F‚Ğ¢W†6WBõ4W'&÷# Ğ¢&WGW&â" Ğ¢–b6—¦RÂ#C Ğ¢&WGW&âb'·6—¦WÔ" Ğ¢–b6—¦RÂ#B¢#C Ğ¢&WGW&âb'·6—¦Rò#C¢ãgÔ² Ğ¢–b6—¦RÂ#B¢#B¢#C Ğ¢&WGW&âb'·6—¦Ròƒ#B¢#B“¢ãgÔÒ Ğ¢&WGW&âb'·6—¦Ròƒ#B¢#B¢#B“¢ãgÔr Ğ 