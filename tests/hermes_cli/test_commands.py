"""Tests for the central command registry and autocomplete."""

import pytest

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from hermes_cli.commands import (
    COMMAND_REGISTRY,
    COMMANDS,
    COMMANDS_BY_CATEGORY,
    CommandDef,
    GATEWAY_KNOWN_COMMANDS,
    SUBCOMMANDS,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
    _CMD_NAME_LIMIT,
    _SLACK_RESERVED_COMMANDS,
    _SLACK_VIA_HERMES_ONLY,
    _TG_NAME_LIMIT,
    _clamp_command_names,
    _clamp_telegram_names,
    _sanitize_telegram_name,
    discord_skill_commands,
    gateway_help_lines,
    resolve_command,
    slack_app_manifest,
    slack_native_slashes,
    slack_subcommand_map,
    telegram_bot_commands,
    telegram_menu_commands,
    telegram_menu_max_commands,
)


def _completions(completer: SlashCommandCompleter, text: str):
    return list(
        completer.get_completions(
            Document(text=text),
            CompleteEvent(completion_requested=True),
        )
    )


# ---------------------------------------------------------------------------
# CommandDef registry tests
# ---------------------------------------------------------------------------

class TestCommandRegistry:


    def test_no_duplicate_canonical_names(self):
        names = [cmd.name for cmd in COMMAND_REGISTRY]
        assert len(names) == len(set(names)), f"Duplicate names: {[n for n in names if names.count(n) > 1]}"

    def test_no_alias_collides_with_canonical_name(self):
        """An alias must not shadow another command's canonical name."""
        canonical_names = {cmd.name for cmd in COMMAND_REGISTRY}
        for cmd in COMMAND_REGISTRY:
            for alias in cmd.aliases:
                if alias in canonical_names:
                    # reset -> new is intentional (reset IS an alias for new)
                    target = next(c for c in COMMAND_REGISTRY if c.name == alias)
                    # This should only happen if the alias points to the same entry
                    assert resolve_command(alias).name == cmd.name or alias == cmd.name, \
                        f"Alias '{alias}' of '{cmd.name}' shadows canonical '{target.name}'"





# ---------------------------------------------------------------------------
# resolve_command tests
# ---------------------------------------------------------------------------

class TestResolveCommand:


    def test_topic_is_gateway_command(self):
        topic = resolve_command("topic")
        assert topic is not None
        assert topic.name == "topic"
        assert "topic" in GATEWAY_KNOWN_COMMANDS

    def test_context_command_registered_with_ctx_alias(self):
        ctx = resolve_command("context")
        assert ctx is not None
        assert ctx.name == "context"
        assert resolve_command("ctx").name == "context"
        assert "all" in (ctx.subcommands or ())
        # Available on both CLI and gateway surfaces
        assert not ctx.cli_only and not ctx.gateway_only
        assert "context" in GATEWAY_KNOWN_COMMANDS




# ---------------------------------------------------------------------------
# Derived dicts (backwards compat)
# ---------------------------------------------------------------------------

class TestDerivedDicts:


    def test_commands_dict_includes_aliases(self):
        assert "/bg" in COMMANDS
        assert "/reset" in COMMANDS
        assert "/q" in COMMANDS
        assert "/exit" in COMMANDS
        assert "/reload_mcp" in COMMANDS
        assert "/gateway" in COMMANDS

    def test_commands_by_category_covers_all_categories(self):
        registry_categories = {cmd.category for cmd in COMMAND_REGISTRY if not cmd.gateway_only}
        assert set(COMMANDS_BY_CATEGORY.keys()) == registry_categories


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------

class TestGatewayKnownCommands:

    def test_includes_config_gated_cli_only(self):
        """Commands with gateway_config_gate are always in GATEWAY_KNOWN_COMMANDS."""
        for cmd in COMMAND_REGISTRY:
            if cmd.gateway_config_gate:
                assert cmd.name in GATEWAY_KNOWN_COMMANDS, \
                    f"config-gated command '{cmd.name}' should be in GATEWAY_KNOWN_COMMANDS"


    def test_is_frozenset(self):
        assert isinstance(GATEWAY_KNOWN_COMMANDS, frozenset)


class TestGatewayHelpLines:

    def test_excludes_cli_only_commands_without_config_gate(self):
        import re
        lines = gateway_help_lines()
        joined = "\n".join(lines)
        for cmd in COMMAND_REGISTRY:
            if cmd.cli_only and not cmd.gateway_config_gate:
                # Word-boundary match so `/reload` doesn't match `/reload-mcp`
                pattern = rf'`/{re.escape(cmd.name)}(?![-_\w])'
                assert not re.search(pattern, joined), \
                    f"cli_only command /{cmd.name} should not be in gateway help"

    def test_includes_alias_note_for_bg(self):
        lines = gateway_help_lines()
        bg_line = [l for l in lines if "/background" in l]
        assert len(bg_line) == 1
        assert "/bg" in bg_line[0]


class TestTelegramBotCommands:
    def test_returns_list_of_tuples(self):
        cmds = telegram_bot_commands()
        assert len(cmds) > 10
        for name, desc in cmds:
            assert isinstance(name, str)
            assert isinstance(desc, str)

    def test_no_hyphens_in_command_names(self):
        """Telegram does not support hyphens in command names."""
        for name, _ in telegram_bot_commands():
            assert "-" not in name, f"Telegram command '{name}' contains a hyphen"


    def test_includes_builtin_commands_with_required_args(self):
        """Built-in arg-taking commands (e.g. /queue, /steer, /background)
        are now included because their handlers return usage text when
        invoked without arguments â€” issue #24312."""
        names = {name for name, _ in telegram_bot_commands()}
        assert "background" in names
        assert "queue" in names
        assert "steer" in names


class TestSlackSubcommandMap:
    def test_returns_dict(self):
        mapping = slack_subcommand_map()
        assert isinstance(mapping, dict)
        assert len(mapping) > 10

    def test_values_are_slash_prefixed(self):
        for key, val in slack_subcommand_map().items():
            assert val.startswith("/"), f"Slack mapping for '{key}' should start with /"


    def test_excludes_cli_only_without_config_gate(self):
        mapping = slack_subcommand_map()
        for cmd in COMMAND_REGISTRY:
            if cmd.cli_only and not cmd.gateway_config_gate:
                assert cmd.name not in mapping


class TestSlackNativeSlashes:
    """Slack native slash command generation â€” used to register every
    COMMAND_REGISTRY entry as a first-class Slack slash, matching Discord
    and Telegram."""



    def test_names_respect_slack_limits(self):
        for name, _desc, _hint in slack_native_slashes():
            # Slack: lowercase a-z, 0-9, hyphens, underscores; max 32 chars
            assert len(name) <= 32, f"slash {name!r} exceeds 32 chars"
            assert name == name.lower()
            for ch in name:
                assert ch.isalnum() or ch in "-_", f"invalid char {ch!r} in {name!r}"





    def test_telegram_parity(self):
        """Every Telegram bot command must be registerable on Slack too.

        This catches the old behavior where Slack users couldn't invoke
        commands like /btw natively. If a future command surfaces on
        Telegram but not Slack (because of Slack's 50-slash cap), this
        test fails loudly so we can curate the list rather than silently
        dropping parity.

        Slack-reserved built-in commands (e.g. /status) are excluded
        from parity checks since they cannot be registered on Slack.
        """
        slack_names = {n for n, _d, _h in slack_native_slashes()}
        tg_names = {n for n, _d in telegram_bot_commands()}
        # Some Telegram names have underscores where Slack uses hyphens
        # (e.g. set_home vs sethome). Normalize both sides for comparison.
        def _norm(s: str) -> str:
            return s.replace("-", "_").replace("__", "_").strip("_")

        slack_norm = {_norm(n) for n in slack_names}
        tg_norm = {_norm(n) for n in tg_names}
        reserved_norm = {_norm(n) for n in _SLACK_RESERVED_COMMANDS}
        # Commands deliberately routed through /hermes <command> on Slack only
        # (Slack's 50-slash cap) are expected to be absent from native slashes.
        via_hermes_norm = {_norm(n) for n in _SLACK_VIA_HERMES_ONLY}
        missing = (tg_norm - slack_norm) - reserved_norm - via_hermes_norm
        assert not missing, (
            f"commands on Telegram but missing from Slack native slashes: {sorted(missing)}"
        )


class TestSlackAppManifest:
    """Generated Slack app manifest (used by `hermes slack manifest`)."""


    def test_each_slash_has_required_fields(self):
        m = slack_app_manifest()
        for entry in m["features"]["slash_commands"]:
            assert entry["command"].startswith("/")
            assert "description" in entry
            assert "url" in entry
            # should_escape must be present (Slack defaults to True which
            # HTML-escapes args â€” we want the raw text)
            assert "should_escape" in entry

    def test_btw_is_in_manifest(self):
        """Regression: /btw must be a native Slack slash, not just a
        /hermes subcommand."""
        m = slack_app_manifest()
        commands = [c["command"] for c in m["features"]["slash_commands"]]
        assert "/btw" in commands


# ---------------------------------------------------------------------------
# Config-gated gateway commands
# ---------------------------------------------------------------------------

class TestGatewayConfigGate:
    """Tests for the gateway_config_gate mechanism on CommandDef."""


    def test_verbose_in_gateway_known_commands(self):
        """Config-gated commands are always recognized by the gateway."""
        assert "verbose" in GATEWAY_KNOWN_COMMANDS

    def test_config_gate_excluded_from_help_when_off(self, tmp_path, monkeypatch):
        """When the config gate is falsy, the command should not appear in help."""
        # Write a config with the gate off (default)
        config_file = tmp_path / "config.yaml"
        config_file.write_text("display:\n  tool_progress_command: false\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        lines = gateway_help_lines()
        joined = "\n".join(lines)
        assert "`/verbose" not in joined


    def test_config_gate_included_in_slack_when_on(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("display:\n  tool_progress_command: true\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        mapping = slack_subcommand_map()
        assert "verbose" in mapping


# ---------------------------------------------------------------------------
# Autocomplete (SlashCommandCompleter)
# ---------------------------------------------------------------------------

class TestSlashCommandCompleter:
    # -- basic prefix completion -----------------------------------------



    # -- exact-match trailing space --------------------------------------


    # -- non-slash input returns nothing ---------------------------------



    # -- skill commands via provider ------------------------------------

    def test_skill_commands_are_completed_from_provider(self):
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/gif-search": {"description": "Search for GIFs across providers"},
            }
        )

        completions = _completions(completer, "/gif")

        assert len(completions) == 1
        assert completions[0].text == "gif-search"
        assert completions[0].display_text == "/gif-search"
        assert completions[0].display_meta_text == "âš¡ Search for GIFs across providers"



    def test_skill_provider_exception_is_swallowed(self):
        """A broken provider should not crash autocomplete."""
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Should return builtin matches only, no crash
        completions = _completions(completer, "/he")
        texts = {item.text for item in completions}
        assert "help" in texts




# â”€â”€ Stacked slash-skill completion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _stacked_completer(**extra_skills):
    skills = {
        "/skill-a": {"description": "Skill A"},
        "/skill-b": {"description": "Skill B"},
        "/skill-c": {"description": "Skill C"},
        **extra_skills,
    }
    return SlashCommandCompleter(skill_commands_provider=lambda: skills)


class TestStackedSkillCompletion:
    """Second+ leading skill tokens keep getting completions (stacked
    slash-skill invocations, Claude Code v2.1.199 port follow-up)."""


    def test_no_completions_for_instruction_text(self):
        assert _completions(_stacked_completer(), "/skill-a do the") == []
        assert _completions(_stacked_completer(), "/skill-a ") == []


    def test_cap_stops_completions(self):
        skills = {f"/stk-{i}": {"description": f"S{i}"} for i in range(8)}
        completer = SlashCommandCompleter(skill_commands_provider=lambda: skills)
        text = " ".join(f"/stk-{i}" for i in range(5)) + " /stk-"
        assert _completions(completer, text) == []


# â”€â”€ SUBCOMMANDS extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSubcommands:
    def test_explicit_subcommands_extracted(self):
        """Commands with explicit subcommands on CommandDef are extracted."""
        assert "/skills" in SUBCOMMANDS
        assert "install" in SUBCOMMANDS["/skills"]


    def test_commands_without_subcommands_not_in_dict(self):
        """Plain commands should not appear in SUBCOMMANDS."""
        assert "/help" not in SUBCOMMANDS
        assert "/quit" not in SUBCOMMANDS
        assert "/clear" not in SUBCOMMANDS


# â”€â”€ Subcommand tab completion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ç~6¶‰žËkºwµçQ¡”É•¹…µ•½¹ÍÑ…¹ÑÌ½™Õ¹Ñ¥½¹ÌÍÑ¥±°•á¥ÍÐÕ¹‘•ÈÑ¡”½±¹…µ•Ì¸ˆˆˆ4(4(€€€‘•˜Ñ•ÍÑ}Ñ}¹…µ•}±¥µ¥Ñ}…±¥…Ì¡Í•±˜¤è4(€€€€€€€…ÍÍ•ÉÐ}Q}95}1%5%P€ôô}5}95}1%5%P€ôô€ÌÈ4(4(€€€‘•˜Ñ•ÍÑ}±…µÁ}Ñ•±•É…µ}¹…µ•Í}¥Í}±…µÁ}½µµ…¹‘}¹…µ•Ì¡Í•±˜¤è4(€€€€€€€…ÍÍ•ÉÐ}±…µÁ}Ñ•±•É…µ}¹…µ•Ì¥Ì}±…µÁ}½µµ…¹‘}¹…µ•Ì4(4(4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(Œ¥Í½ÉÍ­¥±°½µµ…¹É•¥ÍÑÉ…Ñ¥½¸4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4)±…ÍÌQ•ÍÑ¥Í½É‘M­¥±±½µµ…¹‘Ìè4(€€€€ˆˆ‰Q•ÍÑÌ™½È‘¥Í½É‘}Í­¥±±}½µµ…¹‘Ì ¤ƒŠP•¹ÑÉ…±¥é•Í­¥±°É•¥ÍÑÉ…Ñ¥½¸¸ˆˆˆ4(4(4(€€€ÁåÑ•ÍÐ¹µ…É¬¹Ý¥¹‘½ÝÍ}½¹±ä(€€€‘•˜Ñ•ÍÑ}¹…Ñ¥Ù•}Ý¥¹‘½ÝÍ}Á…Ñ¡Í}¥¹±Õ‘•}±½…±}…¹‘}•áÑ•É¹…±}Í­¥±±Ì (€€€€€€€Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ °(€€€€¤è(€€€€€€€€ˆˆ‰	…­Í±…Í µ¹…Ñ¥Ù”Í­¥±°Á…Ñ¡ÌÉ•µ…¥¸Ù¥Í¥‰±”¥¸Ñ¡”™±…Ð½±±•Ñ½È¸ˆˆˆ(€€€€€€€™É½´Õ¹¥ÑÑ•ÍÐ¹µ½¬¥µÁ½ÉÐÁ…Ñ ((€€€€€€€±½…±}‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ(€€€€€€€•áÑ•É¹…±}‘¥È€ôÑµÁ}Á…Ñ €¼€‰•áÑ•É¹…°µÍ­¥±±Ìˆ(€€€€€€€±½½­…±¥­•}‘¥È€ôÑµÁ}Á…Ñ €¼€‰•áÑ•É¹…°µÍ­¥±±Ìµ‰…­ÕÀˆ(€€€€€€€±½…±}‘¥È¹µ­‘¥È ¤(€€€€€€€•áÑ•É¹…±}‘¥È¹µ­‘¥È ¤(€€€€€€€±½½­…±¥­•}‘¥È¹µ­‘¥È ¤(€€€€€€€™…­•}µ‘Ì€ôì(€€€€€€€€€€€€ˆ½±½…°µÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰±½…°µÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰1½…°ˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡±½…±}‘¥È€¼€‰±½…°µÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½•áÑ•É¹…°µÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰•áÑ•É¹…°µÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰áÑ•É¹…°ˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡•áÑ•É¹…±}‘¥È€¼€‰•áÑ•É¹…°µÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½±½½­…±¥­”µÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰±½½­…±¥­”µÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰=ÕÑÍ¥‘”Ñ¡”½¹™¥ÕÉ••áÑ•É¹…°É½½Ðˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ (€€€€€€€€€€€€€€€€€€€±½½­…±¥­•}‘¥È€¼€‰±½½­…±¥­”µÍ­¥±°ˆ€¼€‰M-%10¹µˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½¡ÕˆµÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰¡ÕˆµÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰%¹ÍÑ…±±•‰äÑ¡”¡Õˆˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ (€€€€€€€€€€€€€€€€€€€±½…±}‘¥È€¼€ˆ¹¡Õˆˆ€¼€‰¡ÕˆµÍ­¥±°ˆ€¼€‰M-%10¹µˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½¡Õˆµ‰…­ÕÀµÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰¡Õˆµ‰…­ÕÀµÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰¹½Éµ…°±½…°Í­¥±°ˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ (€€€€€€€€€€€€€€€€€€€±½…±}‘¥È€¼€ˆ¹¡Õˆµ‰…­ÕÀˆ€¼€‰¡Õˆµ‰…­ÕÀµÍ­¥±°ˆ€¼€‰M-%10¹µˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰¹½Ñ¡•È¹½Éµ…°±½…°Í­¥±°ˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ (€€€€€€€€€€€€€€€€€€€±½…±}‘¥È€¼€ˆ¹¡Õˆµ½Ñ¡•Èˆ€¼€‰¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆ€¼€‰M-%10¹µˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô°(€€€€€€€ô((€€€€€€€…ÍÍ•ÉÐ€‰qpˆ¥¸ÍÑÈ¡±½…±}‘¥È¹É•Í½±Ù” ¤¤(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ•¹Ø ‰!I5M}!=5ˆ°ÍÑÈ¡ÑµÁ}Á…Ñ ¤¤(€€€€€€€Ý¥Ñ € (€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¹•Ñ}Í­¥±±}½µµ…¹‘Ìˆ°É•ÑÕÉ¹}Ù…±Õ”õ™…­•}µ‘Ì¤°(€€€€€€€€€€€Á…Ñ  ‰Ñ½½±Ì¹Í­¥±±Í}Ñ½½°¹M-%11M}%Hˆ°±½…±}‘¥È¤°(€€€€€€€€€€€Á…Ñ  (€€€€€€€€€€€€€€€€‰…•¹Ð¹Í­¥±±}ÕÑ¥±Ì¹•Ñ}•áÑ•É¹…±}Í­¥±±Í}‘¥ÉÌˆ°(€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õm•áÑ•É¹…±}‘¥Ét°(€€€€€€€€€€€€¤°(€€€€€€€€¤è(€€€€€€€€€€€•¹ÑÉ¥•Ì°¡¥‘‘•¸€ô‘¥Í½É‘}Í­¥±±}½µµ…¹‘Ì (€€€€€€€€€€€€€€€µ…á}Í±½ÑÌôÔÀ°É•Í•ÉÙ•‘}¹…µ•ÌõÍ•Ð ¤°(€€€€€€€€€€€€¤((€€€€€€€…ÍÍ•ÉÐí¹…µ”™½È¹…µ”°}‘•ÍŒ°}­•ä¥¸•¹ÑÉ¥•Íô€ôôì(€€€€€€€€€€€€‰•áÑ•É¹…°µÍ­¥±°ˆ°(€€€€€€€€€€€€‰¡Õˆµ‰…­ÕÀµÍ­¥±°ˆ°(€€€€€€€€€€€€‰¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆ°(€€€€€€€€€€€€‰±½…°µÍ­¥±°ˆ°(€€€€€€€ô(€€€€€€€…ÍÍ•ÉÐ¡¥‘‘•¸€ôô€À((€€€‘•˜Ñ•ÍÑ}¹…µ•Í}…±±½Ý}¡åÁ¡•¹Ì¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è(€€€€€€€€ˆˆ‰¥Í½É¹…µ•ÌÍ¡½Õ±­••À¡åÁ¡•¹Ì€¡Õ¹±¥­”Q•±•É…´Ì|Í…¹¥Ñ¥é…Ñ¥½¸¤¸ˆˆˆ4(€€€€€€€™É½´Õ¹¥ÑÑ•ÍÐ¹µ½¬¥µÁ½ÉÐÁ…Ñ 4(4(€€€€€€€™…­•}Í­¥±±Í}‘¥È€ôÍÑÈ¡ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤4(€€€€€€€™…­•}µ‘Ì€ôì4(€€€€€€€€€€€€ˆ½µäµ½½°µÍ­¥±°ˆèì4(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰µäµ½½°µÍ­¥±°ˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰½½°Í­¥±°ˆ°4(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆè˜‰í™…­•}Í­¥±±Í}‘¥Éô½µäµ½½°µÍ­¥±°½M-%10¹µˆ°4(€€€€€€€€€€€€€€€€‰Í­¥±±}‘¥Èˆè˜‰í™…­•}Í­¥±±Í}‘¥Éô½µäµ½½°µÍ­¥±°ˆ°4(€€€€€€€€€€€ô°4(€€€€€€€ô4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ•¹Ø ‰!I5M}!=5ˆ°ÍÑÈ¡ÑµÁ}Á…Ñ ¤¤4(€€€€€€€€¡ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤¹µ­‘¥È¡•á¥ÍÑ}½¬õQÉÕ”¤4(€€€€€€€Ý¥Ñ € 4(€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¹•Ñ}Í­¥±±}½µµ…¹‘Ìˆ°É•ÑÕÉ¹}Ù…±Õ”õ™…­•}µ‘Ì¤°4(€€€€€€€€€€€Á…Ñ  ‰Ñ½½±Ì¹Í­¥±±Í}Ñ½½°¹M-%11M}%Hˆ°ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤°4(€€€€€€€€¤è4(€€€€€€€€€€€•¹ÑÉ¥•Ì°|€ô‘¥Í½É‘}Í­¥±±}½µµ…¹‘Ì 4(€€€€€€€€€€€€€€€µ…á}Í±½ÑÌôÔÀ°É•Í•ÉÙ•‘}¹…µ•ÌõÍ•Ð ¤°4(€€€€€€€€€€€€¤4(4(€€€€€€€…ÍÍ•ÉÐ•¹ÑÉ¥•ÍlÁulÁt€ôô€‰µäµ½½°µÍ­¥±°ˆ€€Œ¡åÁ¡•¹ÌÁÉ•Í•ÉÙ•4(4(€€€‘•˜Ñ•ÍÑ}…Á}•¹™½É•µ•¹Ð¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰¹ÑÉ¥•Ì‰•å½¹µ…á}Í±½ÑÌÍ¡½Õ±‰”¡¥‘‘•¸¸ˆˆˆ4(€€€€€€€™É½´Õ¹¥ÑÑ•ÍÐ¹µ½¬¥µÁ½ÉÐÁ…Ñ 4(4(€€€€€€€™…­•}Í­¥±±Í}‘¥È€ôÍÑÈ¡ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤4(€€€€€€€™…­•}µ‘Ì€ôì4(€€€€€€€€€€€˜ˆ½Í­¥±°µí¤èÀÍ‘ôˆèì4(€€€€€€€€€€€€€€€€‰¹…µ”ˆè˜‰Í­¥±°µí¤èÀÍ‘ôˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè˜‰M­¥±°í¥ôˆ°4(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆè˜‰í™…­•}Í­¥±±Í}‘¥Éô½Í­¥±°µí¤èÀÍ‘ô½M-%10¹µˆ°4(€€€€€€€€€€€€€€€€‰Í­¥±±}‘¥Èˆè˜‰í™…­•}Í­¥±±Í}‘¥Éô½Í­¥±°µí¤èÀÍ‘ôˆ°4(€€€€€€€€€€€ô4(€€€€€€€€€€€™½È¤¥¸É…¹” ÈÀ¤4(€€€€€€€ô4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ•¹Ø ‰!I5M}!=5ˆ°ÍÑÈ¡ÑµÁ}Á…Ñ ¤¤4(€€€€€€€€¡ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤¹µ­‘¥È¡•á¥ÍÑ}½¬õQÉÕ”¤4(€€€€€€€Ý¥Ñ € 4(€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¹•Ñ}Í­¥±±}½µµ…¹‘Ìˆ°É•ÑÕÉ¹}Ù…±Õ”õ™…­•}µ‘Ì¤°4(€€€€€€€€€€€Á…Ñ  ‰Ñ½½±Ì¹Í­¥±±Í}Ñ½½°¹M-%11M}%Hˆ°ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤°4(€€€€€€€€¤è4(€€€€€€€€€€€•¹ÑÉ¥•Ì°¡¥‘‘•¸€ô‘¥Í½É‘}Í­¥±±}½µµ…¹‘Ì 4(€€€€€€€€€€€€€€€µ…á}Í±½ÑÌôÔ°É•Í•ÉÙ•‘}¹…µ•ÌõÍ•Ð ¤°4(€€€€€€€€€€€€¤4(4(€€€€€€€…ÍÍ•ÉÐ±•¸¡•¹ÑÉ¥•Ì¤€ôô€Ô4(€€€€€€€…ÍÍ•ÉÐ¡¥‘‘•¸€ôô€ÄÔ4(4(4(4(4(4(4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(Œ¥Í½ÉÍ­¥±°½µµ…¹‘ÌÉ½ÕÁ•‰ä…Ñ•½Éä4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4)™É½´¡•Éµ•Í}±¤¹½µµ…¹‘Ì¥µÁ½ÉÐ‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í}‰å}…Ñ•½Éä€€Œ¹½Å„èÐÀÈ4(4(4)±…ÍÌQ•ÍÑ¥Í½É‘M­¥±±½µµ…¹‘Í	å…Ñ•½Éäè4(€€€€ˆˆ‰Q•ÍÑÌ™½È‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í}‰å}…Ñ•½Éä ¤ƒŠP€½Í­¥±°É½ÕÀÉ•¥ÍÑÉ…Ñ¥½¸¸ˆˆˆ4(4(4(4(4(€€€‘•˜Ñ•ÍÑ}¹½}±•…å|ÈÕàÈÕ}…À¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰Q¡”½±¹•ÍÑ•µ±…å½ÕÐ…ÁÌ€ ÈÔÉ½ÕÁÌƒ\€ÈÔÍ­¥±±Ì½É½ÕÀ¤…É”½¹”¸4(4(€€€€€€€Q¡”±¥Ù”…±±•È™±…ÑÑ•¹Ì…Ñ•½É¥•Ì¥¹Ñ¼„Í¥¹±”…ÕÑ½½µÁ±•Ñ”±¥ÍÐ°4(€€€€€€€Ý¡¥ ¥Í½É™•Ñ¡•Ì‘å¹…µ¥…±±äƒŠPÑ¡”Á•Èµ½µµ…¹€á-Á…å±½…4(€€€€€€€½¹•É¸™É½´Ñ¡”½±¹•ÍÑ•±…å½ÕÐ€ ŒÄÄÌÈÄ°€ŒÄÀÈÔä¤¹¼±½¹•È…ÁÁ±¥•Ì¸4(€€€€€€€Õ…É‘Ì……¥¹ÍÐ…¥‘•¹Ñ…±±äÉ”µ¥¹ÑÉ½‘Õ¥¹œÑ¡”…ÁÌ°Ý¡¥ Ý½Õ±4(€€€€€€€Í¥±•¹Ñ±ä‘É½ÀÍ­¥±±Ì¥¸Ñ¡”€ÈÙÑ ¬…±Á¡…‰•Ñ¥…°…Ñ•½Éä€¡Ñ¡”•á…Ð4(€€€€€€€™…¥±ÕÉ”µ½‘”ÕÍ•ÉÌÝ•É”¡¥ÑÑ¥¹œÝ¥Ñ €Èä…Ñ•½Éä‘¥ÉÌ½¸É•…°4(€€€€€€€¥¹ÍÑ…±±Ì¤¸4(€€€€€€€€ˆˆˆ4(€€€€€€€™É½´Õ¹¥ÑÑ•ÍÐ¹µ½¬¥µÁ½ÉÐÁ…Ñ 4(4(€€€€€€€™…­•}Í­¥±±Í}‘¥È€ôÍÑÈ¡ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤4(4(€€€€€€€€Œ	Õ¥±€ÌÀ…Ñ•½É¥•Ì€ ø½±}5a}I=UALôÈÔ¤•… Ý¥Ñ €ÌÀÍ­¥±±Ì4(€€€€€€€€Œ€ ø½±}5a}AI}I=U@ôÈÔ¤¸4(€€€€€€€™…­•}µ‘Ì€ôíô4(€€€€€€€™½ÈŒ¥¸É…¹” ÌÀ¤è4(€€€€€€€€€€€…Ð€ô˜‰…ÑíŒèÀÉ‘ôˆ€€Œ…ÐÀÀ°…ÐÀÄ°€¸¸¸°…ÐÈäƒŠP€ÌÀ…Ñ•½É¥•Ì4(€€€€€€€€€€€™½ÈÌ¥¸É…¹” ÌÀ¤è4(€€€€€€€€€€€€€€€¹…µ”€ô˜‰Í­¥±°µíŒèÀÉ‘ôµíÌèÀÉ‘ôˆ4(€€€€€€€€€€€€€€€Í­¥±±}ÍÕ‰‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ€¼…Ð€¼¹…µ”4(€€€€€€€€€€€€€€€Í­¥±±}ÍÕ‰‘¥È¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”°•á¥ÍÑ}½¬õQÉÕ”¤4(€€€€€€€€€€€€€€€€¡Í­¥±±}ÍÕ‰‘¥È€¼€‰M-%10¹µˆ¤¹ÝÉ¥Ñ•}Ñ•áÐ ˆ´´µq¹¹…µ”èáq¸´´µq¸ˆ¤4(€€€€€€€€€€€€€€€™…­•}µ‘Ím˜ˆ½í¹…µ•ô‰t€ôì4(€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆè¹…µ”°4(€€€€€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè˜‰…Ñ•½Éäí…ÑôÍ­¥±°íÍôˆ°4(€€€€€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆè˜‰í™…­•}Í­¥±±Í}‘¥Éô½í…Ñô½í¹…µ•ô½M-%10¹µˆ°4(€€€€€€€€€€€€€€€ô4(4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ•¹Ø ‰!I5M}!=5ˆ°ÍÑÈ¡ÑµÁ}Á…Ñ ¤¤4(€€€€€€€Ý¥Ñ € 4(€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¹•Ñ}Í­¥±±}½µµ…¹‘Ìˆ°É•ÑÕÉ¹}Ù…±Õ”õ™…­•}µ‘Ì¤°4(€€€€€€€€€€€Á…Ñ  ‰Ñ½½±Ì¹Í­¥±±Í}Ñ½½°¹M-%11M}%Hˆ°ÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ¤°4(€€€€€€€€¤è4(€€€€€€€€€€€…Ñ•½É¥•Ì°Õ¹…Ñ•½É¥é•°¡¥‘‘•¸€ô‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í}‰å}…Ñ•½Éä 4(€€€€€€€€€€€€€€€É•Í•ÉÙ•‘}¹…µ•ÌõÍ•Ð ¤°4(€€€€€€€€€€€€¤4(4(€€€€€€€€ŒÙ•Éä…Ñ•½ÉäÍ¡½Õ±‰”ÁÉ•Í•¹ÐƒŠP¹¼€ÈÔµÉ½ÕÀ…À4(€€€€€€€…ÍÍ•ÉÐ±•¸¡…Ñ•½É¥•Ì¤€ôô€ÌÀ°€ 4(€€€€€€€€€€€˜‰•áÁ•Ñ•…±°€ÌÀ…Ñ•½É¥•Ì°½Ðí±•¸¡…Ñ•½É¥•Ì¥ô€ˆ4(€€€€€€€€€€€˜ˆ¡…À™É½´½±¹•ÍÑ•±…å½ÕÐµÕÍÐ‰”É•µ½Ù•¤ˆ4(€€€€€€€€¤4(€€€€€€€€ŒÙ•ÉäÍ­¥±°¥¸•Ù•Éä…Ñ•½ÉäµÕÍÐ‰”ÁÉ•Í•¹ÐƒŠP¹¼€ÈÔµÁ•ÈµÉ½ÕÀ…À4(€€€€€€€™½È…Ñ}¹…µ”°•¹ÑÉ¥•Ì¥¸…Ñ•½É¥•Ì¹¥Ñ•µÌ ¤è4(€€€€€€€€€€€…ÍÍ•ÉÐ±•¸¡•¹ÑÉ¥•Ì¤€ôô€ÌÀ°€ 4(€€€€€€€€€€€€€€€˜‰…Ñ•½Éäí…Ñ}¹…µ•ôè•áÁ•Ñ•€ÌÀÍ­¥±±Ì°½Ðí±•¸¡•¹ÑÉ¥•Ì¥ô€ˆ4(€€€€€€€€€€€€€€€˜ˆ¡…À™É½´½±¹•ÍÑ•±…å½ÕÐµÕÍÐ‰”É•µ½Ù•¤ˆ4(€€€€€€€€€€€€¤4(€€€€€€€€Œ9½Ñ¡¥¹œÍ¡½Õ±‰”É•Á½ÉÑ•¡¥‘‘•¸™½ÈÑ¡”…ÀÉ•…Í½¸€¡Ñ¡”½¹±ä4(€€€€€€€€Œ±•¥Ñ¥µ…Ñ”¡¥‘‘•¸É•…Í½¸¹½Ü¥Ì¹…µ”±…µÀ½±±¥Í¥½¹Ì°Ý¡¥ 4(€€€€€€€€Œ‘½¸Ð¡…ÁÁ•¸¡•É”Í¥¹”…±°¹…µ•Ì…É”Õ¹¥ÅÕ”¤¸4(€€€€€€€…ÍÍ•ÉÐ¡¥‘‘•¸€ôô€À4(4(€€€‘•˜Ñ•ÍÑ}•áÑ•É¹…±}‘¥ÉÍ}Í­¥±±Í}¥¹±Õ‘•¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è(€€€€€€€€ˆˆ‰M­¥±±Ì¥¸Í­¥±±Ì¹•áÑ•É¹…±}‘¥ÉÍ€µÕÍÐ…ÁÁ•…È¥¸€½Í­¥±°…ÕÑ½½µÁ±•Ñ”¸4(4(€€€€€€€€ŒÄàÜÐÄ™¥á•Ñ¡¥Ì™½ÈÑ¡”™±…Ð‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í€½±±•Ñ½È4(€€€€€€€‰ÕÐ±•™Ð‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í}‰å}…Ñ•½Éå€€¡Ñ¡”±¥Ù”…±±•È™½È4(€€€€€€€¥Í½ÉÌ€½Í­¥±±€½µµ…¹¤ÍÑ¥±°™¥±Ñ•É¥¹œ‰ä4(€€€€€€€M-%11M}%I€ÁÉ•™¥à½¹±ä¸I•É•ÍÍ¥½¸Õ…ÉÑ¡…Ð‰½Ñ ½±±•Ñ½ÉÌ4(€€€€€€€¹½Ü…•ÁÐ•áÑ•É¹…°µ‘¥ÈÍ­¥±±Ì¸4(€€€€€€€€ˆˆˆ4(€€€€€€€™É½´Õ¹¥ÑÑ•ÍÐ¹µ½¬¥µÁ½ÉÐÁ…Ñ 4(4(€€€€€€€±½…±}Í­¥±±Í}‘¥È€ôÑµÁ}Á…Ñ €¼€‰±½…°µÍ­¥±±Ìˆ4(€€€€€€€•áÑ•É¹…±}‘¥È€ôÑµÁ}Á…Ñ €¼€‰•áÑ•É¹…°µÍ­¥±±Ìˆ4(4(€€€€€€€€¡±½…±}Í­¥±±Í}‘¥È€¼€‰É•…Ñ¥Ù”ˆ€¼€‰±½…°µÍ­¥±°ˆ¤¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”¤4(€€€€€€€€¡±½…±}Í­¥±±Í}‘¥È€¼€‰É•…Ñ¥Ù”ˆ€¼€‰±½…°µÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤¹ÝÉ¥Ñ•}Ñ•áÐ ˆˆ¤4(4(€€€€€€€€¡•áÑ•É¹…±}‘¥È€¼€‰µ±½ÁÌˆ€¼€‰•áÑ•É¹…°µÍ­¥±°ˆ¤¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”¤4(€€€€€€€€¡•áÑ•É¹…±}‘¥È€¼€‰µ±½ÁÌˆ€¼€‰•áÑ•É¹…°µÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤¹ÝÉ¥Ñ•}Ñ•áÐ ˆˆ¤4(4(€€€€€€€™…­•}µ‘Ì€ôì4(€€€€€€€€€€€€ˆ½±½…°µÍ­¥±°ˆèì4(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰±½…°µÍ­¥±°ˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰1½…°ˆ°4(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡±½…±}Í­¥±±Í}‘¥È€¼€‰É•…Ñ¥Ù”ˆ€¼€‰±½…°µÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€ˆ½•áÑ•É¹…°µÍ­¥±°ˆèì4(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰•áÑ•É¹…°µÍ­¥±°ˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰áÑ•É¹…°ˆ°4(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡•áÑ•É¹…±}‘¥È€¼€‰µ±½ÁÌˆ€¼€‰•áÑ•É¹…°µÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°4(€€€€€€€€€€€ô°4(€€€€€€€ô4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ•¹Ø ‰!I5M}!=5ˆ°ÍÑÈ¡ÑµÁ}Á…Ñ ¤¤4(€€€€€€€Ý¥Ñ € 4(€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¹•Ñ}Í­¥±±}½µµ…¹‘Ìˆ°É•ÑÕÉ¹}Ù…±Õ”õ™…­•}µ‘Ì¤°4(€€€€€€€€€€€Á…Ñ  ‰Ñ½½±Ì¹Í­¥±±Í}Ñ½½°¹M-%11M}%Hˆ°±½…±}Í­¥±±Í}‘¥È¤°4(€€€€€€€€€€€Á…Ñ  4(€€€€€€€€€€€€€€€€‰…•¹Ð¹Í­¥±±}ÕÑ¥±Ì¹•Ñ}•áÑ•É¹…±}Í­¥±±Í}‘¥ÉÌˆ°4(€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õm•áÑ•É¹…±}‘¥Ét°4(€€€€€€€€€€€€¤°4(€€€€€€€€¤è4(€€€€€€€€€€€…Ñ•½É¥•Ì°Õ¹…Ñ•½É¥é•°¡¥‘‘•¸€ô‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í}‰å}…Ñ•½Éä 4(€€€€€€€€€€€€€€€É•Í•ÉÙ•‘}¹…µ•ÌõÍ•Ð ¤°4(€€€€€€€€€€€€¤4(4(€€€€€€€€Œ1½…°Í­¥±°ƒŠHÉ½ÕÁ•Õ¹‘•È€‰É•…Ñ¥Ù”ˆ4(€€€€€€€…ÍÍ•ÉÐ€‰É•…Ñ¥Ù”ˆ¥¸…Ñ•½É¥•Ì4(€€€€€€€…ÍÍ•ÉÐ…¹ä¡¸€ôô€‰±½…°µÍ­¥±°ˆ™½È¸°}°}¬¥¸…Ñ•½É¥•Íl‰É•…Ñ¥Ù”‰t¤4(€€€€€€€€ŒáÑ•É¹…°Í­¥±°ƒŠHÉ½ÕÁ•Õ¹‘•È¥ÑÌ½Ý¸Ñ½Àµ±•Ù•°‘¥È€‰µ±½ÁÌˆ4(€€€€€€€…ÍÍ•ÉÐ€‰µ±½ÁÌˆ¥¸…Ñ•½É¥•Ì°€ 4(€€€€€€€€€€€€‰•áÑ•É¹…°µ‘¥ÈÍ­¥±±ÌµÕÍÐ‰”¥¹±Õ‘•ƒŠPÑ¡”½±M-%11M}%Hµ½¹±ä€ˆ4(€€€€€€€€€€€€‰ÁÉ•™¥à¡•¬Ý…Ì‰É½­•¸™½È‰å}…Ñ•½Éä€¡½µÁ±•Ñ•Ì€ŒÄàÜÐÄ¤ˆ4(€€€€€€€€¤4(€€€€€€€…ÍÍ•ÉÐ…¹ä¡¸€ôô€‰•áÑ•É¹…°µÍ­¥±°ˆ™½È¸°}°}¬¥¸…Ñ•½É¥•Íl‰µ±½ÁÌ‰t¤4(€€€€€€€…ÍÍ•ÉÐÕ¹…Ñ•½É¥é•€ôômt(€€€€€€€…ÍÍ•ÉÐ¡¥‘‘•¸€ôô€À((€€€‘•˜Ñ•ÍÑ}¡Õ‰}•á±ÕÍ¥½¹}¥Í}Á…Ñ¡}½µÁ½¹•¹Ñ}…Ý…É”¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è(€€€€€€€€ˆˆ‰¥Í½É…Ñ•½É¥•Ì•á±Õ‘”€¹¡Õˆ°¹½ÐÍ¥µ¥±…É±ä¹…µ•Í¥‰±¥¹Ì¸ˆˆˆ(€€€€€€€™É½´Õ¹¥ÑÑ•ÍÐ¹µ½¬¥µÁ½ÉÐÁ…Ñ ((€€€€€€€±½…±}‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í­¥±±Ìˆ(€€€€€€€™…­•}µ‘Ì€ôì(€€€€€€€€€€€€ˆ½¡ÕˆµÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰¡ÕˆµÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰%¹ÍÑ…±±•‰äÑ¡”¡Õˆˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡±½…±}‘¥È€¼€ˆ¹¡Õˆˆ€¼€‰¡ÕˆµÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½¡Õˆµ‰…­ÕÀµÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰¡Õˆµ‰…­ÕÀµÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰¹½Éµ…°±½…°Í­¥±°ˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡±½…±}‘¥È€¼€ˆ¹¡Õˆµ‰…­ÕÀˆ€¼€‰¡Õˆµ‰…­ÕÀµÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€ˆ½¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆèì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè€‰¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆ°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰¹½Ñ¡•È¹½Éµ…°±½…°Í­¥±°ˆ°(€€€€€€€€€€€€€€€€‰Í­¥±±}µ‘}Á…Ñ ˆèÍÑÈ¡±½…±}‘¥È€¼€ˆ¹¡Õˆµ½Ñ¡•Èˆ€¼€‰¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆ€¼€‰M-%10¹µˆ¤°(€€€€€€€€€€€ô°(€€€€€€€ô((€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ•¹Ø ‰!I5M}!=5ˆ°ÍÑÈ¡ÑµÁ}Á…Ñ ¤¤(€€€€€€€Ý¥Ñ € (€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¹•Ñ}Í­¥±±}½µµ…¹‘Ìˆ°É•ÑÕÉ¹}Ù…±Õ”õ™…­•}µ‘Ì¤°(€€€€€€€€€€€Á…Ñ  ‰Ñ½½±Ì¹Í­¥±±Í}Ñ½½°¹M-%11M}%Hˆ°±½…±}‘¥È¤°(€€€€€€€€€€€Á…Ñ  ‰…•¹Ð¹Í­¥±±}ÕÑ¥±Ì¹•Ñ}•áÑ•É¹…±}Í­¥±±Í}‘¥ÉÌˆ°É•ÑÕÉ¹}Ù…±Õ”õmt¤°(€€€€€€€€¤è(€€€€€€€€€€€…Ñ•½É¥•Ì°Õ¹…Ñ•½É¥é•°¡¥‘‘•¸€ô‘¥Í½É‘}Í­¥±±}½µµ…¹‘Í}‰å}…Ñ•½Éä (€€€€€€€€€€€€€€€É•Í•ÉÙ•‘}¹…µ•ÌõÍ•Ð ¤°(€€€€€€€€€€€€¤((€€€€€€€…ÍÍ•ÉÐ€ˆ¹¡Õˆˆ¹½Ð¥¸…Ñ•½É¥•Ì(€€€€€€€…ÍÍ•ÉÐ…¹ä¡¹…µ”€ôô€‰¡Õˆµ‰…­ÕÀµÍ­¥±°ˆ™½È¹…µ”°}‘•ÍŒ°}­•ä¥¸…Ñ•½É¥•Ílˆ¹¡Õˆµ‰…­ÕÀ‰t¤(€€€€€€€…ÍÍ•ÉÐ…¹ä¡¹…µ”€ôô€‰¡Õˆµ½Ñ¡•ÈµÍ­¥±°ˆ™½È¹…µ”°}‘•ÍŒ°}­•ä¥¸…Ñ•½É¥•Ílˆ¹¡Õˆµ½Ñ¡•È‰t¤(€€€€€€€…ÍÍ•ÉÐÕ¹…Ñ•½É¥é•€ôômt(€€€€€€€…ÍÍ•ÉÐ¡¥‘‘•¸€ôô€À(4(4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(ŒA±Õ¥¸Í±…Í ½µµ…¹¥¹Ñ•É…Ñ¥½¸4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4)±…ÍÌQ•ÍÑA±Õ¥¹½µµ…¹‘¹Õµ•É…Ñ¥½¸è4(€€€€ˆˆ‰A±Õ¥¸½µµ…¹‘ÌÉ•¥ÍÑ•É•Ù¥„Ñà¹É•¥ÍÑ•É}½µµ…¹ ¤µÕÍÐ‰”ÍÕÉ™…•4(€€€‰ä•Ù•Éä…Ñ•Ý…ä•¹Õµ•É…Ñ½È€¡Q•±•É…´µ•¹Ô°M±…¬ÍÕ‰½µµ…¹µ…À°•ÑŒ¸¤¸4(€€€€ˆˆˆ4(4(€€€‘•˜}Á…Ñ¡}Á±Õ¥¹}½µµ…¹‘Ì¡Í•±˜°µ½¹­•åÁ…Ñ °½µµ…¹‘Ì¤è4(€€€€€€€€ˆˆ‰5½¹­•åÁ…Ñ ¡•Éµ•Í}±¤¹Á±Õ¥¹Ì¹•Ñ}Á±Õ¥¹}½µµ…¹‘Ì ¤Ñ¼„™¥á•‘¥Ð¸ˆˆˆ4(€€€€€€€™É½´¡•Éµ•Í}±¤¥µÁ½ÉÐÁ±Õ¥¹Ì…Ì}Á±Õ¥¹Í}µ½4(4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ 4(€€€€€€€€€€€}Á±Õ¥¹Í}µ½°€‰•Ñ}Á±Õ¥¹}½µµ…¹‘Ìˆ°±…µ‰‘„è‘¥Ð¡½µµ…¹‘Ì¤4(€€€€€€€€¤4(4(4(4(€€€‘•˜Ñ•ÍÑ}Á±Õ¥¹}½µµ…¹‘}Ý¥Ñ¡}¡åÁ¡•¹Í}Í…¹¥Ñ¥é•‘}™½É}Ñ•±•É…´¡Í•±˜°µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰A±Õ¥¸¹…µ•Ì½¹Ñ…¥¹¥¹œ¡åÁ¡•¹ÌµÕÍÐ‰”Õ¹‘•ÉÍ½É”µ¹½Éµ…±¥é•™½ÈQ•±•É…´¸ˆˆˆ4(€€€€€€€Í•±˜¹}Á…Ñ¡}Á±Õ¥¹}½µµ…¹‘Ì¡µ½¹­•åÁ…Ñ °ì4(€€€€€€€€€€€€‰µäµÁ±Õ¥¸µµˆèì4(€€€€€€€€€€€€€€€€‰¡…¹‘±•Èˆè±…µ‰‘„}„è€‰½¬ˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€‰‘•ÍŒˆ°4(€€€€€€€€€€€€€€€€‰…ÉÍ}¡¥¹Ðˆè€ˆˆ°4(€€€€€€€€€€€€€€€€‰Á±Õ¥¸ˆè€‰Àˆ°4(€€€€€€€€€€€ô4(€€€€€€€ô¤4(€€€€€€€¹…µ•Ì€ôí¹…µ”™½È¹…µ”°}‘•ÍŒ¥¸Ñ•±•É…µ}‰½Ñ}½µµ…¹‘Ì ¥ô4(€€€€€€€…ÍÍ•ÉÐ€‰µå}Á±Õ¥¹}µˆ¥¸¹…µ•Ì4(€€€€€€€…ÍÍ•ÉÐ€‰µäµÁ±Õ¥¸µµˆ¹½Ð¥¸¹…µ•Ì4(4(4(4(€€€‘•˜Ñ•ÍÑ}Á±Õ¥¹}•¹Õµ•É…Ñ½É}¡…¹‘±•Í}µ¥ÍÍ¥¹}Á±Õ¥¹}µ…¹…•È¡Í•±˜°µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰¹Õµ•É…Ñ½ÉÌµÕÍÐ¹•Ù•ÈÉ…¥Í”Ý¡•¸Á±Õ¥¸‘¥Í½Ù•ÉäÉ…¥Í•Ì¸ˆˆˆ4(€€€€€€€™É½´¡•Éµ•Í}±¤¥µÁ½ÉÐÁ±Õ¥¹Ì…Ì}Á±Õ¥¹Í}µ½4(4(€€€€€€€‘•˜}‰½½´ ¤è4(€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È ‰Á±Õ¥¸ÍåÍÑ•´‘½Ý¸ˆ¤4(4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡}Á±Õ¥¹Í}µ½°€‰•Ñ}Á±Õ¥¹}½µµ…¹‘Ìˆ°}‰½½´¤4(4(€€€€€€€€Œ	½Ñ …±±ÌÍ¡½Õ±ÍÕ••…¹©ÕÍÐÉ•ÑÕÉ¸Ñ¡”‰Õ¥±Ðµ¥¸Í•Ð¸4(€€€€€€€Ñ}¹…µ•Ì€ôí¹…µ”™½È¹…µ”°}‘•ÍŒ¥¸Ñ•±•É…µ}‰½Ñ}½µµ…¹‘Ì ¥ô4(€€€€€€€Í±…­}¹…µ•Ì€ôÍ•Ð¡Í±…­}ÍÕ‰½µµ…¹‘}µ…À ¤¤4(€€€€€€€…ÍÍ•ÉÐ€‰ÍÑ…ÑÕÌˆ¥¸Ñ}¹…µ•Ì4(€€€€€€€…ÍÍ•ÉÐ€‰ÍÑ…ÑÕÌˆ¥¸Í±…­}¹…µ•Ì4(