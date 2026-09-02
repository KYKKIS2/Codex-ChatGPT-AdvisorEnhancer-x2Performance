#!/usr/bin/env python3
"""Regression tests for the optional ChatGPT Project g4f GUI bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image as PILImage
from PIL.PngImagePlugin import PngInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "codex-skill" / "external-advisor" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import advisor_cloud_catalog as catalog
import advisor_gui


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def encoded(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fake_auth(account: str = "account-a", user: str = "user-a") -> dict[str, object]:
    payload = {
        "sub": user,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account,
            "chatgpt_account_user_id": user,
        },
    }
    token = f"{encoded({'alg': 'none'})}.{encoded(payload)}.signature"
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "user_id": "device-id",
    }


def graph(
    conversation_id: str,
    final_message_id: str = "assistant-final",
    user_prompt: str = "Inspect the repository.",
    prior_message_id: str | None = None,
) -> dict[str, object]:
    mapping: dict[str, object] = {
        "root": {"id": "root", "parent": None, "message": None},
    }
    user_parent = "root"
    if prior_message_id:
        mapping.update({
            "prior-user": {
                "id": "prior-user",
                "parent": "root",
                "message": {
                    "id": "prior-user",
                    "author": {"role": "user"},
                    "content": {"parts": ["Earlier turn."]},
                    "status": "finished_successfully",
                    "end_turn": True,
                    "create_time": 8,
                },
            },
            prior_message_id: {
                "id": prior_message_id,
                "parent": "prior-user",
                "message": {
                    "id": prior_message_id,
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Earlier answer."]},
                    "status": "finished_successfully",
                    "end_turn": True,
                    "create_time": 9,
                },
            },
        })
        user_parent = prior_message_id
    mapping.update({
        "user-one": {
            "id": "user-one",
            "parent": user_parent,
            "message": {
                "id": "user-one",
                "author": {"role": "user"},
                "content": {"parts": [user_prompt]},
                "status": "finished_successfully",
                "end_turn": True,
                "create_time": 10,
            },
        },
        "progress": {
            "id": "progress",
            "parent": "user-one",
            "message": {
                "id": "progress",
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "thoughts",
                    "thoughts": [{
                        "summary": "Inspected repository files",
                        "content": "private hidden reasoning",
                        "finished": True,
                        "chunks": [],
                    }],
                },
                "metadata": {"summary_type": "raw_cot"},
                "status": "finished_successfully",
                "end_turn": False,
                "create_time": 11,
            },
        },
        "tool-one": {
            "id": "tool-one",
            "parent": "progress",
            "message": {
                "id": "tool-one",
                "author": {"role": "tool"},
                "content": {"parts": ["private tool output"]},
                "status": "finished_successfully",
                "end_turn": False,
                "create_time": 12,
            },
        },
        final_message_id: {
            "id": final_message_id,
            "parent": "tool-one",
            "message": {
                "id": final_message_id,
                "author": {"role": "assistant"},
                "content": {"parts": ["The inspection is complete."]},
                "status": "finished_successfully",
                "end_turn": True,
                "create_time": 13,
            },
        },
    })
    return {
        "id": conversation_id,
        "conversation_id": conversation_id,
        "current_node": final_message_id,
        "mapping": mapping,
    }


def cloud_request(project_key: str, conversation_key: str) -> dict[str, object]:
    return {
        "provider": "OpenaiAccount",
        "model": "gpt-5-6-thinking",
        "thinking_effort": "max",
        "messages": [{"role": "user", "content": "Continue the analysis."}],
        "conversation": {
            "advisor_cloud_project": project_key,
            "advisor_cloud_handle": conversation_key,
        },
    }


def prompt_sha256(prompt: str = "Inspect the repository.") -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class AdvisorGuiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "ADVISOR_GUI_STATE_DIR": str(self.root / "state"),
                "ADVISOR_RUNTIME_DIR": str(self.root / "runtime"),
                "ADVISOR_REMOTE_START_INTERVAL_SECONDS": "0",
            },
        )
        self.env.start()
        self.auth = fake_auth()
        self.project = self.root / "project"
        self.project.mkdir()
        self.binding = {
            "chatgpt_project_id": "g-p-project123",
            "name": "PolyMarket Research",
        }
        binding_path = self.project / ".codex-advisor" / "project.json"
        binding_path.parent.mkdir()
        binding_path.write_text(json.dumps(self.binding), encoding="utf-8")

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def register(self) -> tuple[str, str]:
        project_key = catalog.register_bound_project(self.project, auth=self.auth)
        public = catalog.sync_conversations(
            project_key,
            [{
                "id": "conversation-raw-123",
                "title": "Bound cloud chat",
                "create_time": 100,
                "update_time": 200,
            }],
            self.auth,
        )
        return project_key, public[0]["key"]

    def test_catalog_is_private_account_bound_and_public_payload_is_opaque(self) -> None:
        project_key, conversation_key = self.register()
        projects = catalog.list_projects(self.auth)
        conversations = catalog.sync_conversations(
            project_key,
            [{"id": "conversation-raw-123", "title": "Cloud title", "update_time": 300}],
            self.auth,
        )
        public_text = json.dumps({"projects": projects, "conversations": conversations})
        self.assertNotIn("g-p-project123", public_text)
        self.assertNotIn("conversation-raw-123", public_text)
        self.assertNotIn(str(self.project), public_text)
        self.assertIn(project_key, public_text)
        self.assertIn(conversation_key, public_text)

        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(catalog.state_root().stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(catalog.catalog_path().stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((catalog.state_root() / "catalog.key").stat().st_mode), 0o600)

        with self.assertRaises(catalog.AccountMismatchError):
            catalog.list_projects(fake_auth("account-b", "user-b"))

    def test_same_project_binding_deduplicates_multiple_local_paths(self) -> None:
        first = catalog.register_bound_project(self.project, auth=self.auth)
        sibling = self.root / "sibling"
        sibling.mkdir()
        binding = sibling / ".codex-advisor" / "project.json"
        binding.parent.mkdir()
        binding.write_text(json.dumps(self.binding), encoding="utf-8")
        second = catalog.register_bound_project(sibling, auth=self.auth)
        self.assertEqual(first, second)
        projects = catalog.list_projects(self.auth)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["registeredPaths"], 2)

    def test_browser_import_includes_visible_activity_but_omits_private_payloads(self) -> None:
        project_key, conversation_key = self.register()
        record = catalog.conversation_record(project_key, conversation_key, self.auth)
        data = graph("conversation-raw-123")
        data["mapping"]["user-one"]["message"]["content"] = {
            "content_type": "multimodal_text",
            "parts": [
                {"asset_pointer": "file-service://private-image-id"},
                "Inspect the repository.",
            ],
        }
        data["mapping"]["user-one"]["message"]["metadata"] = {
            "attachments": [{
                "id": "private-image-id",
                "mimeType": "image/png",
                "name": "private-local-name.png",
            }],
        }
        payload = advisor_gui.browser_conversation(
            project_key,
            conversation_key,
            record,
            data,
        )
        self.assertEqual(
            [item["role"] for item in payload["items"]],
            ["user", "assistant", "assistant"],
        )
        self.assertEqual(payload["items"][1]["kind"], "activity")
        self.assertEqual(payload["items"][1]["content"], "Inspected repository files")
        self.assertEqual(payload["items"][0]["imageCount"], 1)
        serialized = json.dumps(payload)
        self.assertNotIn("conversation-raw-123", serialized)
        self.assertNotIn("g-p-project123", serialized)
        self.assertNotIn("assistant-final", serialized)
        self.assertNotIn("private tool output", serialized)
        self.assertNotIn("private hidden reasoning", serialized)
        self.assertNotIn("private-image-id", serialized)
        self.assertNotIn("private-local-name.png", serialized)
        self.assertEqual(
            payload["data"]["OpenaiAccount"]["advisor_cloud_handle"],
            conversation_key,
        )
        self.assertFalse(payload["advisorCloud"]["continuationFromTool"])

    def test_live_activity_returns_only_current_turn_summaries(self) -> None:
        project_key, conversation_key = self.register()
        submission_nonce = "a" * 32
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-old",
                "parent_message_id": "assistant-old",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            submission_nonce,
            prompt_sha256=prompt_sha256(),
        )
        current = graph(
            "conversation-raw-123",
            "assistant-new",
            prior_message_id="assistant-old",
        )
        current["mapping"]["old-progress"] = {
            "id": "old-progress",
            "parent": "prior-user",
            "message": {
                "id": "old-progress",
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "thoughts",
                    "thoughts": [{"summary": "Old turn activity", "content": "old private reasoning"}],
                },
                "metadata": {"summary_type": "raw_cot"},
                "status": "finished_successfully",
                "end_turn": False,
                "create_time": 8.5,
            },
        }
        current["mapping"]["assistant-old"]["parent"] = "old-progress"
        with mock.patch.object(advisor_gui, "fetch_remote_conversation_once", return_value=current) as fetch:
            activities = advisor_gui.live_conversation_activities(
                project_key,
                conversation_key,
                self.auth,
                submission_nonce,
            )
        self.assertEqual(activities, ["Inspected repository files"])
        self.assertEqual(fetch.call_count, 1)
        serialized = json.dumps(activities)
        self.assertNotIn("Inspect the repository", serialized)
        self.assertNotIn("private hidden reasoning", serialized)
        self.assertNotIn("private tool output", serialized)
        self.assertNotIn("Old turn activity", serialized)
        self.assertNotIn("old private reasoning", serialized)

        with mock.patch.object(advisor_gui, "fetch_remote_conversation_once") as wrong_turn_fetch:
            self.assertEqual(
                advisor_gui.live_conversation_activities(
                    project_key,
                    conversation_key,
                    self.auth,
                    "b" * 32,
                ),
                [],
            )
        wrong_turn_fetch.assert_not_called()

        catalog.clear_submission_after_refresh(project_key, conversation_key, self.auth)
        with mock.patch.object(advisor_gui, "fetch_remote_conversation_once") as no_fetch:
            self.assertEqual(
                advisor_gui.live_conversation_activities(
                    project_key,
                    conversation_key,
                    self.auth,
                    submission_nonce,
                ),
                [],
            )
        no_fetch.assert_not_called()

    def test_live_activity_rate_limit_is_not_retried_server_side(self) -> None:
        limited = advisor_gui.advisor.RateLimitError("rate limited", retry_after=12.0)
        with (
            mock.patch.object(advisor_gui.advisor, "get_json", side_effect=limited) as get_json,
            mock.patch.object(advisor_gui.concurrency, "record_remote_rate_limit") as record,
        ):
            with self.assertRaises(advisor_gui.GuiActivityRateLimited) as raised:
                advisor_gui.fetch_remote_conversation_once("conversation-raw-123", self.auth)

        get_json.assert_called_once()
        record.assert_called_once_with(12.0)
        self.assertEqual(raised.exception.retry_after, 60.0)

    def test_remote_state_contains_all_openai_chat_continuation_fields(self) -> None:
        state = advisor_gui.remote_state_from_data(
            "conversation-raw-123",
            graph("conversation-raw-123"),
            self.auth,
        )
        self.assertEqual(state["conversation_id"], "conversation-raw-123")
        self.assertEqual(state["message_id"], "assistant-final")
        self.assertEqual(state["parent_message_id"], "assistant-final")
        self.assertEqual(state["user_id"], "device-id")
        self.assertEqual(state["recipient"], "all")
        self.assertFalse(state["is_thinking"])
        self.assertEqual(state["thoughts_summary"], "")
        for name in ("finish_reason", "p", "prompt", "generated_images", "task"):
            self.assertIsNone(state[name])

    def test_remote_state_preserves_terminal_tool_as_exact_continuation_parent(self) -> None:
        data = graph("conversation-raw-123")
        data["mapping"]["terminal-tool"] = {
            "id": "terminal-tool",
            "parent": "assistant-final",
            "message": {
                "id": "terminal-tool",
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "content": {"content_type": "text", "parts": []},
                "status": "finished_successfully",
                "end_turn": True,
                "create_time": 14,
            },
        }
        data["current_node"] = "terminal-tool"

        state = advisor_gui.remote_state_from_data(
            "conversation-raw-123",
            data,
            self.auth,
        )
        self.assertEqual(state["message_id"], "terminal-tool")
        self.assertEqual(state["parent_message_id"], "terminal-tool")

        project_key, conversation_key = self.register()
        payload = advisor_gui.browser_conversation(
            project_key,
            conversation_key,
            catalog.conversation_record(project_key, conversation_key, self.auth),
            data,
        )
        self.assertTrue(payload["advisorCloud"]["continuationFromTool"])

        with mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=data):
            reconciled = advisor_gui.fetch_reconciled_conversation(
                "conversation-raw-123",
                self.auth,
                "terminal-tool",
                attempts=1,
                interval=0,
            )
        self.assertIs(reconciled, data)

    def test_submission_stays_blocked_until_explicit_refresh(self) -> None:
        project_key, conversation_key = self.register()
        with self.assertRaises(catalog.CatalogError):
            catalog.begin_submission(
                project_key,
                conversation_key,
                self.auth,
                "invalid-nonce",
                prompt_sha256="invalid",
            )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        self.assertTrue(catalog.submission_pending(project_key, conversation_key, self.auth))
        with self.assertRaises(catalog.CatalogError):
            catalog.begin_submission(
                project_key,
                conversation_key,
                self.auth,
                "nonce-two",
                prompt_sha256=prompt_sha256(),
            )
        catalog.clear_submission_after_refresh(project_key, conversation_key, self.auth)
        self.assertFalse(catalog.submission_pending(project_key, conversation_key, self.auth))

    def test_provider_finish_preserves_journal_until_remote_status_is_complete(self) -> None:
        project_key, conversation_key = self.register()
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-old",
                "parent_message_id": "assistant-old",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        catalog.bind_submission_user_message(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            "user-new",
        )
        bound = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertEqual(bound["submission"]["user_message_id"], "user-new")
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-new",
                "parent_message_id": "assistant-new",
            },
        )

        with mock.patch.object(
            advisor_gui.advisor,
            "remote_conversation_stream_status",
            return_value="IS_STREAMING",
        ):
            completed = advisor_gui._finish_submission_if_remote_complete(
                project_key,
                conversation_key,
                "conversation-raw-123",
                self.auth,
                "nonce-one",
            )
        self.assertFalse(completed)
        self.assertTrue(catalog.submission_pending(project_key, conversation_key, self.auth))

        with mock.patch.object(
            advisor_gui.advisor,
            "remote_conversation_stream_status",
            return_value="COMPLETE",
        ):
            completed = advisor_gui._finish_submission_if_remote_complete(
                project_key,
                conversation_key,
                "conversation-raw-123",
                self.auth,
                "nonce-one",
            )
        self.assertTrue(completed)
        record = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertNotIn("submission", record)
        self.assertEqual(record["reconcile_message_id"], "assistant-new")

    def test_pending_observer_renders_safe_progress_without_clearing_journal(self) -> None:
        project_key, conversation_key = self.register()
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-old",
                "parent_message_id": "assistant-old",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        current = graph(
            "conversation-raw-123",
            "assistant-new",
            prior_message_id="assistant-old",
        )
        current["mapping"]["assistant-new"]["message"].update({
            "content": {"content_type": "text", "parts": ["Partial visible answer."]},
            "status": "in_progress",
            "end_turn": False,
        })
        with (
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="IS_STREAMING",
            ),
            mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=current),
        ):
            observed = advisor_gui.observe_pending_conversation(
                project_key,
                conversation_key,
                self.auth,
            )

        self.assertEqual(observed["status"], "streaming")
        self.assertEqual(observed["conversation"]["items"][-1]["content"], "Partial visible answer.")
        serialized = json.dumps(observed)
        self.assertIn("Inspected repository files", serialized)
        self.assertNotIn("private hidden reasoning", serialized)
        self.assertNotIn("private tool output", serialized)
        self.assertTrue(catalog.submission_pending(project_key, conversation_key, self.auth))

        catalog.clear_submission_after_refresh(project_key, conversation_key, self.auth)
        with (
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="IS_STREAMING",
            ),
            mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=current),
        ):
            external = advisor_gui.observe_pending_conversation(
                project_key,
                conversation_key,
                self.auth,
            )
        self.assertEqual(external["status"], "streaming")
        self.assertEqual(external["conversation"]["items"][-1]["content"], "Partial visible answer.")

    def test_stale_post_finish_import_cannot_regress_cloud_parent(self) -> None:
        project_key, conversation_key = self.register()
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-old",
                "parent_message_id": "assistant-old",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-new",
                "parent_message_id": "assistant-new",
            },
        )
        catalog.finish_submission(project_key, conversation_key, self.auth, "nonce-one")
        pending = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertEqual(pending["reconcile_message_id"], "assistant-new")

        stale = graph("conversation-raw-123", "assistant-old")
        current = graph(
            "conversation-raw-123",
            "assistant-new",
            prior_message_id="assistant-old",
        )
        with mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=stale):
            with self.assertRaises(advisor_gui.GuiBridgeError):
                advisor_gui.fetch_reconciled_conversation(
                    "conversation-raw-123",
                    self.auth,
                    "assistant-new",
                    attempts=1,
                    interval=0,
                )

        with (
            mock.patch.object(advisor_gui, "_conversation_is_still_in_project"),
            mock.patch.object(advisor_gui, "require_complete_conversation"),
            mock.patch.object(
                advisor_gui,
                "fetch_remote_conversation",
                side_effect=[stale, current],
            ) as fetch,
            mock.patch.object(advisor_gui.time, "sleep"),
        ):
            imported = advisor_gui.import_conversation(
                project_key,
                conversation_key,
                self.auth,
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(imported["items"][-1]["content"], "The inspection is complete.")
        reconciled = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertEqual(reconciled["message_id"], "assistant-new")
        self.assertNotIn("reconcile_message_id", reconciled)

    def test_ambiguous_submission_import_waits_for_new_final_and_clears_journal(self) -> None:
        project_key, conversation_key = self.register()
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-old",
                "parent_message_id": "assistant-old",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        current = graph(
            "conversation-raw-123",
            "assistant-new",
            prior_message_id="assistant-old",
        )
        with (
            mock.patch.object(advisor_gui, "_conversation_is_still_in_project"),
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                side_effect=["IS_STREAMING", "COMPLETE"],
            ) as status,
            mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=current) as fetch,
            mock.patch.object(advisor_gui.time, "sleep") as sleep,
        ):
            imported = advisor_gui.import_conversation(
                project_key,
                conversation_key,
                self.auth,
            )
        self.assertEqual(status.call_count, 2)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(sleep.call_count, 1)
        self.assertTrue(imported["recoveredSubmission"])
        reconciled = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertEqual(reconciled["message_id"], "assistant-new")
        self.assertNotIn("submission", reconciled)

    def test_accepted_submission_without_final_preserves_terminal_tool_context(self) -> None:
        project_key, conversation_key = self.register()
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "tool-parent",
                "parent_message_id": "tool-parent",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        catalog.bind_submission_user_message(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            "user-one",
        )

        current = graph(
            "conversation-raw-123",
            "assistant-unused",
            prior_message_id="assistant-old",
        )
        current["mapping"]["tool-parent"] = {
            "id": "tool-parent",
            "parent": "assistant-old",
            "message": {
                "id": "tool-parent",
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "content": {"content_type": "text", "parts": []},
                "status": "finished_successfully",
                "end_turn": True,
                "create_time": 9.5,
            },
        }
        current["mapping"]["user-one"]["parent"] = "tool-parent"
        current["mapping"]["terminal-tool"] = {
            "id": "terminal-tool",
            "parent": "tool-one",
            "message": {
                "id": "terminal-tool",
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "content": {"content_type": "text", "parts": []},
                "status": "finished_successfully",
                "end_turn": True,
                "create_time": 13,
            },
        }
        current["current_node"] = "terminal-tool"

        with (
            mock.patch.object(advisor_gui, "_conversation_is_still_in_project"),
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="COMPLETE",
            ),
            mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=current),
        ):
            imported = advisor_gui.import_conversation(
                project_key,
                conversation_key,
                self.auth,
            )

        self.assertTrue(imported["recoveredSubmission"])
        self.assertTrue(imported["advisorCloud"]["continuationFromTool"])
        reconciled = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertEqual(reconciled["message_id"], "terminal-tool")
        self.assertEqual(reconciled["parent_message_id"], "terminal-tool")
        self.assertNotIn("submission", reconciled)

    def test_ambiguous_submission_rejects_unrelated_graph_advancement(self) -> None:
        unrelated = graph(
            "conversation-raw-123",
            "assistant-unrelated",
            user_prompt="A different tab submitted this.",
            prior_message_id="assistant-old",
        )
        with (
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="COMPLETE",
            ),
            mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=unrelated),
        ):
            with self.assertRaises(advisor_gui.GuiBridgeError):
                advisor_gui.fetch_ambiguous_submission_result(
                    "conversation-raw-123",
                    self.auth,
                    "assistant-old",
                    prompt_sha256(),
                    attempts=1,
                    interval=0,
                )

        matching_text_wrong_identity = graph(
            "conversation-raw-123",
            "assistant-unrelated",
            prior_message_id="assistant-old",
        )
        with (
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="COMPLETE",
            ),
            mock.patch.object(
                advisor_gui,
                "fetch_remote_conversation",
                return_value=matching_text_wrong_identity,
            ),
        ):
            with self.assertRaises(advisor_gui.GuiCloudHistoryPending):
                advisor_gui.fetch_ambiguous_submission_result(
                    "conversation-raw-123",
                    self.auth,
                    "assistant-old",
                    prompt_sha256(),
                    "different-user-node",
                    attempts=1,
                    interval=0,
                )

    def test_ambiguous_submission_with_unchanged_graph_remains_blocked(self) -> None:
        project_key, conversation_key = self.register()
        catalog.update_remote_state(
            project_key,
            conversation_key,
            self.auth,
            {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-old",
                "parent_message_id": "assistant-old",
            },
        )
        catalog.begin_submission(
            project_key,
            conversation_key,
            self.auth,
            "nonce-one",
            prompt_sha256=prompt_sha256(),
        )
        stale = graph("conversation-raw-123", "assistant-old")
        with (
            mock.patch.dict(
                os.environ,
                {
                    "ADVISOR_GUI_RECOVERY_ATTEMPTS": "2",
                    "ADVISOR_GUI_RECOVERY_INTERVAL": "0.1",
                },
            ),
            mock.patch.object(advisor_gui, "_conversation_is_still_in_project"),
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="COMPLETE",
            ) as status,
            mock.patch.object(advisor_gui, "fetch_remote_conversation", return_value=stale) as fetch,
            mock.patch.object(advisor_gui.time, "sleep"),
        ):
            with self.assertRaises(advisor_gui.GuiBridgeError):
                advisor_gui.import_conversation(
                    project_key,
                    conversation_key,
                    self.auth,
                )
        self.assertEqual(status.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(catalog.submission_pending(project_key, conversation_key, self.auth))

    def test_sse_rewrite_updates_private_state_and_scrubs_identifiers(self) -> None:
        project_key, conversation_key = self.register()
        tracker = {"state": False, "finish": False, "error": False}
        event = (
            "event: conversation\n"
            "data: "
            + json.dumps({
                "type": "conversation",
                "conversation": {
                    "OpenaiAccount": {
                        "conversation_id": "conversation-raw-123",
                        "message_id": "assistant-new",
                        "parent_message_id": "assistant-new",
                        "user_id": "device-id",
                    }
                },
            })
            + "\n\n"
        )
        rewritten = advisor_gui.rewrite_sse_chunk(
            event,
            provider="OpenaiAccount",
            project_key=project_key,
            conversation_key=conversation_key,
            auth=self.auth,
            expected_conversation_id="conversation-raw-123",
            sensitive_values={"conversation-raw-123", "assistant-new", "g-p-project123"},
            tracker=tracker,
        )
        self.assertNotIn("conversation-raw-123", rewritten)
        self.assertNotIn("assistant-new", rewritten)
        self.assertIn(conversation_key, rewritten)
        self.assertTrue(tracker["state"])
        private = catalog.conversation_record(project_key, conversation_key, self.auth)
        self.assertEqual(private["message_id"], "assistant-new")

        reasoning_event = "data: " + json.dumps({
            "type": "reasoning",
            "token": "",
            "status": "Inspected repository files",
        }) + "\n\n"
        activity = advisor_gui.rewrite_sse_chunk(
            reasoning_event,
            provider="OpenaiAccount",
            project_key=project_key,
            conversation_key=conversation_key,
            auth=self.auth,
            expected_conversation_id="conversation-raw-123",
            sensitive_values=set(),
            tracker=tracker,
        )
        self.assertIn('"type":"activity"', activity)
        self.assertIn("Inspected repository files", activity)

        adversarial_reasoning = advisor_gui.rewrite_sse_chunk(
            {
                "type": "reasoning",
                "token": "private reasoning token",
                "status": "Looks user-visible",
                "response": {"path": "/private/repository"},
            },
            provider="OpenaiAccount",
            project_key=project_key,
            conversation_key=conversation_key,
            auth=self.auth,
            expected_conversation_id="conversation-raw-123",
            sensitive_values=set(),
            tracker=tracker,
        )
        self.assertEqual(adversarial_reasoning, "")

        request_event = "data: " + json.dumps({
            "type": "request",
            "request": {"conversation_id": "conversation-raw-123", "gizmo_id": "g-p-project123"},
        }) + "\n\n"
        scrubbed = advisor_gui.rewrite_sse_chunk(
            request_event,
            provider="OpenaiAccount",
            project_key=project_key,
            conversation_key=conversation_key,
            auth=self.auth,
            expected_conversation_id="conversation-raw-123",
            sensitive_values={"conversation-raw-123", "g-p-project123"},
            tracker=tracker,
        )
        self.assertNotIn("conversation-raw-123", scrubbed)
        self.assertNotIn("g-p-project123", scrubbed)
        self.assertEqual(scrubbed, "\n")

        response_event = "data: " + json.dumps({
            "type": "response",
            "response": {"output": "private tool output", "path": "/private/repository"},
        }) + "\n\n"
        dropped = advisor_gui.rewrite_sse_chunk(
            response_event,
            provider="OpenaiAccount",
            project_key=project_key,
            conversation_key=conversation_key,
            auth=self.auth,
            expected_conversation_id="conversation-raw-123",
            sensitive_values=set(),
            tracker=tracker,
        )
        self.assertNotIn("private tool output", dropped)
        self.assertNotIn("/private/repository", dropped)

        error_event = "data: " + json.dumps({
            "type": "message",
            "error": "upstream failed",
        }) + "\n\n"
        advisor_gui.rewrite_sse_chunk(
            error_event,
            provider="OpenaiAccount",
            project_key=project_key,
            conversation_key=conversation_key,
            auth=self.auth,
            expected_conversation_id="conversation-raw-123",
            sensitive_values=set(),
            tracker=tracker,
        )
        self.assertTrue(tracker["error"])

    def test_local_gui_assets_are_self_contained_and_avoid_unsafe_dom_sinks(self) -> None:
        html = (SCRIPTS / "advisor_cloud_gui.html").read_text(encoding="utf-8")
        javascript = (SCRIPTS / "advisor_cloud_gui.js").read_text(encoding="utf-8")
        stylesheet = (SCRIPTS / "advisor_cloud_gui.css").read_text(encoding="utf-8")
        for source in (html, javascript, stylesheet):
            local_mathml = source.replace("http://www.w3.org/1998/Math/MathML", "")
            self.assertNotIn("http://", local_mathml)
            self.assertNotIn("https://", source)
            self.assertNotIn("g4f.dev", source)
            self.assertNotIn("g4f.space", source)
        for sink in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "localStorage",
            "sessionStorage",
            "indexedDB",
        ):
            self.assertNotIn(sink, javascript)
        self.assertIn('payload.type === "activity"', javascript)
        self.assertIn("MATHML_NAMESPACE", javascript)
        self.assertIn("createElementNS", javascript)
        self.assertIn("createTable", javascript)
        self.assertIn('document.createElement("table")', javascript)
        self.assertIn("sidebar-collapsed", javascript)
        self.assertIn("recoveredSubmission", javascript)
        self.assertIn("Recovering cloud conversation", javascript)
        self.assertIn("pollConversationActivity", javascript)
        self.assertIn("recoverConversationUntilReady", javascript)
        self.assertIn("observeConversation", javascript)
        self.assertIn("remote_turn_running", javascript)
        self.assertIn("ChatGPT is still working", javascript)
        self.assertIn("Previous turn ended without a final answer", javascript)
        self.assertIn("continuationFromTool", javascript)
        self.assertIn("liveActivityIndex", javascript)
        self.assertIn("/activity", javascript)
        self.assertIn("/observe", javascript)
        self.assertIn("X-Advisor-Activity-Token", javascript)
        self.assertIn("5000", javascript)
        self.assertIn("Retry-After", javascript)
        self.assertIn("activity_rate_limited", javascript)
        self.assertIn("60000", javascript)
        self.assertIn("new FormData", javascript)
        self.assertIn('addEventListener("paste"', javascript)
        self.assertIn("addImageFiles", javascript)
        self.assertIn('id="image-input"', html)
        self.assertIn('accept="image/jpeg,image/png,image/webp,image/gif"', html)
        self.assertIn('id="attachment-tray"', html)
        self.assertIn(".activity-row", stylesheet)
        self.assertIn(".turn-state.working", stylesheet)
        self.assertIn(".turn-state.warning", stylesheet)
        self.assertIn(".attachment-preview", stylesheet)
        self.assertIn(".message-attachments", stylesheet)
        self.assertIn(".math-display", stylesheet)
        self.assertIn(".markdown-table-scroll", stylesheet)
        self.assertIn("body.sidebar-collapsed", stylesheet)

    def test_loopback_shell_security_headers_and_route_surface(self) -> None:
        try:
            from flask import Flask, Response
        except ImportError as exc:  # pragma: no cover - setup installs Flask
            self.skipTest(str(exc))
        app = Flask(__name__)

        @app.get("/chat/")
        def chat() -> Response:
            response = Response(
                '<html><head><script src="chat.v1.js"></script></head><body></body></html>',
                mimetype="text/html",
            )
            response.direct_passthrough = True
            return response

        @app.post("/backend-api/v2/conversation", endpoint="_handle_conversation")
        def conversation() -> str:
            return "ok"

        advisor_gui.install_advisor_routes(app, SCRIPTS)
        client = app.test_client()
        allowed = client.get("/chat/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(allowed.status_code, 200)
        body = allowed.get_data(as_text=True)
        self.assertIn("Advisor Cloud", body)
        self.assertIn('src="/advisor-cloud.js"', body)
        self.assertIn('href="/advisor-cloud.css"', body)
        self.assertNotIn("chat.v1.js", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)
        self.assertIn("default-src 'none'", allowed.headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", allowed.headers["Content-Security-Policy"])
        self.assertIn("img-src 'self' data: blob:", allowed.headers["Content-Security-Policy"])
        self.assertEqual(allowed.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(allowed.headers["X-Frame-Options"], "DENY")
        self.assertEqual(allowed.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(allowed.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(allowed.headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("no-store", allowed.headers["Cache-Control"])
        allowed.close()
        with (
            mock.patch.object(advisor_gui, "require_auth", return_value=self.auth),
            mock.patch.object(
                advisor_gui,
                "live_conversation_activities",
                return_value=["Inspected repository files"],
            ),
        ):
            activity = client.get(
                "/advisor-api/projects/project/conversations/conversation/activity",
                headers={
                    "Origin": "http://localhost",
                    "X-Advisor-Activity-Token": "a" * 32,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
            cross_origin = client.get(
                "/advisor-api/projects/project/conversations/conversation/activity",
                headers={
                    "Origin": "https://example.com",
                    "X-Advisor-Activity-Token": "a" * 32,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
            missing_token = client.get(
                "/advisor-api/projects/project/conversations/conversation/activity",
                headers={"Origin": "http://localhost"},
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
        self.assertEqual(activity.status_code, 200)
        self.assertEqual(activity.get_json(), {"activities": ["Inspected repository files"]})
        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(missing_token.status_code, 400)
        with (
            mock.patch.object(advisor_gui, "require_auth", return_value=self.auth),
            mock.patch.object(
                advisor_gui,
                "live_conversation_activities",
                side_effect=advisor_gui.GuiActivityRateLimited(12),
            ) as limited_activity,
        ):
            rate_limited = client.get(
                "/advisor-api/projects/project/conversations/conversation/activity",
                headers={
                    "Origin": "http://localhost",
                    "X-Advisor-Activity-Token": "a" * 32,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
        self.assertEqual(rate_limited.status_code, 429)
        self.assertEqual(rate_limited.get_json()["error"]["code"], "activity_rate_limited")
        self.assertEqual(rate_limited.headers["Retry-After"], "60")
        limited_activity.assert_called_once()
        with mock.patch.object(
            advisor_gui,
            "import_conversation",
            side_effect=advisor_gui.GuiRemoteTurnRunning("The selected ChatGPT conversation is still running."),
        ):
            running = client.post(
                "/advisor-api/projects/project/conversations/conversation/import",
                headers={"Origin": "http://localhost", "X-Advisor-Cloud": "1"},
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
        self.assertEqual(running.status_code, 409)
        self.assertEqual(running.get_json()["error"]["code"], "remote_turn_running")
        self.assertEqual(client.get("/private/").status_code, 404)
        self.assertEqual(client.get("/backend-api/v2/models").status_code, 404)
        denied = client.get("/chat/", environ_base={"REMOTE_ADDR": "203.0.113.10"})
        self.assertEqual(denied.status_code, 403)

    def test_cloud_send_rejects_unmarked_or_unexpected_requests_before_provider(self) -> None:
        try:
            from flask import Flask
        except ImportError as exc:  # pragma: no cover - setup installs Flask
            self.skipTest(str(exc))
        project_key, conversation_key = self.register()
        app = Flask(__name__)
        provider_calls = 0

        @app.post("/backend-api/v2/conversation", endpoint="_handle_conversation")
        def conversation() -> str:
            nonlocal provider_calls
            provider_calls += 1
            return "unexpected"

        advisor_gui.install_advisor_routes(app, SCRIPTS)
        client = app.test_client()
        base = cloud_request(project_key, conversation_key)
        request_context = {
            "headers": {"Origin": "http://localhost", "X-Advisor-Cloud": "1"},
            "environ_base": {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
        }
        missing_marker = client.post(
            "/backend-api/v2/conversation",
            json=base,
            headers={"Origin": "http://localhost"},
            environ_base=request_context["environ_base"],
        )
        self.assertEqual(missing_marker.status_code, 403)

        invalid_model = dict(base, model="default")
        self.assertEqual(
            client.post("/backend-api/v2/conversation", json=invalid_model, **request_context).status_code,
            400,
        )
        unexpected_field = dict(base, web_search=True)
        self.assertEqual(
            client.post("/backend-api/v2/conversation", json=unexpected_field, **request_context).status_code,
            400,
        )
        invalid_messages = dict(base, messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ])
        self.assertEqual(
            client.post("/backend-api/v2/conversation", json=invalid_messages, **request_context).status_code,
            400,
        )
        self.assertEqual(provider_calls, 0)

    def test_cloud_send_rejects_invalid_or_oversized_images_before_provider(self) -> None:
        try:
            from flask import Flask
        except ImportError as exc:  # pragma: no cover - setup installs Flask
            self.skipTest(str(exc))
        project_key, conversation_key = self.register()
        app = Flask(__name__)
        provider_calls = 0

        @app.post("/backend-api/v2/conversation", endpoint="_handle_conversation")
        def conversation() -> str:
            nonlocal provider_calls
            provider_calls += 1
            return "unexpected"

        advisor_gui.install_advisor_routes(app, SCRIPTS)
        client = app.test_client()
        headers = {"Origin": "http://localhost", "X-Advisor-Cloud": "1"}
        environ = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"}
        payload = json.dumps(cloud_request(project_key, conversation_key))

        invalid = client.post(
            "/backend-api/v2/conversation",
            data={"json": payload, "files": (BytesIO(b"not an image"), "image.png", "image/png")},
            headers=headers,
            environ_base=environ,
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("valid supported image", invalid.get_data(as_text=True))

        with mock.patch.object(advisor_gui, "MAX_IMAGE_UPLOAD_BYTES", 4):
            oversized = client.post(
                "/backend-api/v2/conversation",
                data={"json": payload, "files": (BytesIO(PNG_BYTES), "image.png", "image/png")},
                headers=headers,
                environ_base=environ,
                content_type="multipart/form-data",
            )
        self.assertEqual(oversized.status_code, 413)

        animated_buffer = BytesIO()
        first_frame = PILImage.new("RGB", (2, 2), "red")
        second_frame = PILImage.new("RGB", (2, 2), "blue")
        try:
            first_frame.save(
                animated_buffer,
                format="GIF",
                save_all=True,
                append_images=[second_frame],
                duration=100,
                loop=0,
            )
        finally:
            first_frame.close()
            second_frame.close()
        animated = client.post(
            "/backend-api/v2/conversation",
            data={
                "json": payload,
                "files": (BytesIO(animated_buffer.getvalue()), "animated.gif", "image/gif"),
            },
            headers=headers,
            environ_base=environ,
            content_type="multipart/form-data",
        )
        self.assertEqual(animated.status_code, 400)
        self.assertIn("Animated image uploads", animated.get_data(as_text=True))
        self.assertEqual(provider_calls, 0)

    def test_cloud_send_validates_multipart_image_and_strips_local_filename(self) -> None:
        try:
            from flask import Flask, Response, request
        except ImportError as exc:  # pragma: no cover - setup installs Flask
            self.skipTest(str(exc))
        project_key, conversation_key = self.register()
        app = Flask(__name__)
        captured: dict[str, object] = {}

        @app.post("/backend-api/v2/conversation", endpoint="_handle_conversation")
        def conversation() -> Response:
            captured["body"] = json.loads(request.form["json"])
            upload = request.files.getlist("files")[0]
            captured["filename"] = upload.filename
            captured["content_type"] = upload.mimetype
            captured["bytes"] = upload.read()
            return Response([
                {
                    "type": "request",
                    "request": {
                        "messages": [{"id": "user-new", "author": {"role": "user"}}],
                    },
                },
                {
                    "type": "conversation",
                    "conversation": {
                        "OpenaiAccount": {
                            "conversation_id": "conversation-raw-123",
                            "message_id": "assistant-new",
                            "parent_message_id": "assistant-new",
                        },
                    },
                },
                {"type": "finish", "finish": "stop"},
            ], mimetype="text/event-stream")

        private_state = {
            "conversation_id": "conversation-raw-123",
            "project_id": "g-p-project123",
            "message_id": "assistant-final",
        }
        prepared = {
            **cloud_request(project_key, conversation_key),
            "conversation": {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-final",
                "parent_message_id": "assistant-final",
            },
            "gizmo_id": "g-p-project123",
            "temporary": False,
        }
        remote_context = mock.MagicMock()
        remote_lease = mock.MagicMock()
        remote_context.__enter__.return_value = remote_lease
        conversation_lease = mock.MagicMock()
        metadata_buffer = BytesIO()
        metadata = PngInfo()
        metadata.add_text("Comment", "private-local-metadata")
        metadata_image = PILImage.new("RGB", (2, 2), "green")
        try:
            metadata_image.save(metadata_buffer, format="PNG", pnginfo=metadata)
        finally:
            metadata_image.close()
        with (
            mock.patch.object(advisor_gui, "require_auth", return_value=self.auth),
            mock.patch.object(advisor_gui, "_prepare_cloud_turn", return_value=(prepared, private_state)),
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="COMPLETE",
            ),
            mock.patch.object(advisor_gui.concurrency, "remote_call_slot", return_value=remote_context),
            mock.patch.object(
                advisor_gui.concurrency,
                "ConversationLockLease",
                return_value=conversation_lease,
            ),
        ):
            advisor_gui.install_advisor_routes(app, SCRIPTS)
            response = app.test_client().post(
                "/backend-api/v2/conversation",
                data={
                    "json": json.dumps(cloud_request(project_key, conversation_key)),
                    "files": (
                        BytesIO(metadata_buffer.getvalue()),
                        "private-local-name.png",
                        "image/png",
                    ),
                },
                headers={"Origin": "http://localhost", "X-Advisor-Cloud": "1"},
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
                content_type="multipart/form-data",
            )
            response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["filename"], "image-1.png")
        self.assertEqual(captured["content_type"], "image/png")
        self.assertNotIn(b"private-local-metadata", captured["bytes"])
        with PILImage.open(BytesIO(captured["bytes"])) as normalized:
            self.assertEqual(normalized.format, "PNG")
            self.assertEqual(normalized.size, (2, 2))
            self.assertNotIn("Comment", normalized.info)
        self.assertEqual(captured["body"]["gizmo_id"], "g-p-project123")
        self.assertNotIn("private-local-name.png", json.dumps(captured["body"]))
        remote_lease.mark_start.assert_called_once_with()
        conversation_lease.release.assert_called()

    def test_cloud_turn_modes_match_the_direct_g4f_transport_contract(self) -> None:
        project_key, conversation_key = self.register()
        thinking = cloud_request(project_key, conversation_key)
        self.assertEqual(
            advisor_gui._validate_cloud_turn_body(thinking),
            ("OpenaiAccount", project_key, conversation_key),
        )
        pro = dict(thinking, model="gpt-5-6-pro", thinking_effort="standard")
        self.assertEqual(
            advisor_gui._validate_cloud_turn_body(pro),
            ("OpenaiAccount", project_key, conversation_key),
        )
        with self.assertRaises(advisor_gui.GuiBridgeError):
            advisor_gui._validate_cloud_turn_body(
                dict(thinking, model="gpt-5-6-pro", thinking_effort="pro-extended")
            )

    def test_bound_project_registration_never_modifies_binding(self) -> None:
        path = self.project / ".codex-advisor" / "project.json"
        before = path.read_bytes()
        catalog.register_bound_project(self.project, auth=self.auth)
        self.assertEqual(path.read_bytes(), before)

    def test_cloud_send_releases_coordination_after_unexpected_failure(self) -> None:
        try:
            from flask import Flask
        except ImportError as exc:  # pragma: no cover - setup installs Flask
            self.skipTest(str(exc))
        project_key, conversation_key = self.register()
        app = Flask(__name__)

        @app.post("/backend-api/v2/conversation", endpoint="_handle_conversation")
        def conversation() -> str:
            raise ValueError("private implementation failure")

        class FakeRemoteLease:
            def mark_start(self) -> None:
                raise AssertionError("A failed pre-stream request must not mark a remote turn start.")

        class FakeRemoteContext:
            def __init__(self) -> None:
                self.exited = False

            def __enter__(self) -> FakeRemoteLease:
                return FakeRemoteLease()

            def __exit__(self, *_args: object) -> None:
                self.exited = True

        class FakeConversationLease:
            def __init__(self, **_kwargs: object) -> None:
                self.released = False

            def acquire_key(self, _key: str) -> None:
                return None

            def release(self) -> None:
                self.released = True

        remote_context = FakeRemoteContext()
        conversation_lease = FakeConversationLease()
        private_state = {
            "conversation_id": "conversation-raw-123",
            "project_id": "g-p-project123",
            "message_id": "assistant-final",
        }
        with (
            mock.patch.object(advisor_gui, "require_auth", return_value=self.auth),
            mock.patch.object(advisor_gui, "_prepare_cloud_turn", return_value=({}, private_state)),
            mock.patch.object(advisor_gui.concurrency, "remote_call_slot", return_value=remote_context),
            mock.patch.object(
                advisor_gui.concurrency,
                "ConversationLockLease",
                return_value=conversation_lease,
            ),
        ):
            advisor_gui.install_advisor_routes(app, SCRIPTS)
            response = app.test_client().post(
                "/backend-api/v2/conversation",
                json=cloud_request(project_key, conversation_key),
                headers={"Origin": "http://localhost", "X-Advisor-Cloud": "1"},
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private implementation failure", response.get_data(as_text=True))
        self.assertTrue(conversation_lease.released)
        self.assertTrue(remote_context.exited)

    def test_cloud_send_scrubs_ids_and_completes_submission_journal(self) -> None:
        try:
            from flask import Flask, Response, request
        except ImportError as exc:  # pragma: no cover - setup installs Flask
            self.skipTest(str(exc))
        project_key, conversation_key = self.register()
        app = Flask(__name__)
        captured: dict[str, object] = {}

        @app.post("/backend-api/v2/conversation", endpoint="_handle_conversation")
        def conversation() -> Response:
            captured.update(request.get_json())
            events = [
                {"type": "provider", "provider": "OpenaiAccount"},
                {
                    "type": "request",
                    "request": {
                        "messages": [{"id": "user-new", "author": {"role": "user"}}],
                    },
                },
                {
                    "type": "reasoning",
                    "token": "",
                    "status": "Inspected repository files",
                },
                {
                    "type": "response",
                    "response": {"output": "private tool output"},
                },
                {
                    "type": "conversation",
                    "conversation": {
                        "OpenaiAccount": {
                            "conversation_id": "conversation-raw-123",
                            "message_id": "assistant-new",
                            "parent_message_id": "assistant-new",
                            "user_id": "device-id",
                        }
                    },
                },
                {"type": "finish", "finish": "stop"},
            ]
            return Response(events, mimetype="text/event-stream")

        class FakeRemoteLease:
            def __init__(self) -> None:
                self.started = False

            def mark_start(self) -> None:
                self.started = True

        class FakeRemoteContext:
            def __init__(self) -> None:
                self.lease = FakeRemoteLease()
                self.exited = False

            def __enter__(self) -> FakeRemoteLease:
                return self.lease

            def __exit__(self, *_args: object) -> None:
                self.exited = True

        class FakeConversationLease:
            def __init__(self, **_kwargs: object) -> None:
                self.released = False

            def acquire_key(self, _key: str) -> None:
                return None

            def release(self) -> None:
                self.released = True

        remote_context = FakeRemoteContext()
        conversation_lease = FakeConversationLease()
        private_state = {
            "conversation_id": "conversation-raw-123",
            "project_id": "g-p-project123",
            "message_id": "assistant-final",
        }
        prepared = {
            "provider": "OpenaiAccount",
            "conversation": {
                "conversation_id": "conversation-raw-123",
                "message_id": "assistant-final",
                "parent_message_id": "assistant-final",
            },
            "gizmo_id": "g-p-project123",
        }
        with (
            mock.patch.object(advisor_gui, "require_auth", return_value=self.auth),
            mock.patch.object(advisor_gui, "_prepare_cloud_turn", return_value=(prepared, private_state)),
            mock.patch.object(
                advisor_gui.advisor,
                "remote_conversation_stream_status",
                return_value="COMPLETE",
            ),
            mock.patch.object(advisor_gui.concurrency, "remote_call_slot", return_value=remote_context),
            mock.patch.object(
                advisor_gui.concurrency,
                "ConversationLockLease",
                return_value=conversation_lease,
            ),
        ):
            advisor_gui.install_advisor_routes(app, SCRIPTS)
            response = app.test_client().post(
                "/backend-api/v2/conversation",
                json=cloud_request(project_key, conversation_key),
                headers={"Origin": "http://localhost", "X-Advisor-Cloud": "1"},
                environ_base={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost"},
            )
            body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        activity_token = response.headers.get("X-Advisor-Activity-Token", "")
        self.assertEqual(len(activity_token), 32)
        self.assertTrue(all(character in "0123456789abcdef" for character in activity_token))
        self.assertEqual(captured["gizmo_id"], "g-p-project123")
        self.assertNotIn("conversation-raw-123", body)
        self.assertNotIn("g-p-project123", body)
        self.assertNotIn("assistant-new", body)
        self.assertNotIn("device-id", body)
        self.assertNotIn("private reasoning token", body)
        self.assertNotIn("private tool output", body)
        self.assertIn("Inspected repository files", body)
        self.assertIn(conversation_key, body)
        self.assertTrue(remote_context.lease.started)
        self.assertTrue(conversation_lease.released)
        self.assertTrue(remote_context.exited)
        self.assertFalse(catalog.submission_pending(project_key, conversation_key, self.auth))


if __name__ == "__main__":
    unittest.main()
