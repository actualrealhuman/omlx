"""Regression guards for the chat UI overhaul follow-up."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_TEMPLATE = ROOT / "omlx/admin/templates/chat.html"
I18N_DIR = ROOT / "omlx/admin/i18n"
TAILWIND_CSS = ROOT / "omlx/admin/static/css/tailwind.css"

NEW_I18N_KEYS = {
    "chat.clear_search",
    "chat.no_chats_match",
    "chat.pin_chat",
    "chat.unpin_chat",
    "chat.regenerate_creative",
    "chat.regenerate_with",
    "chat.chat_tab",
    "chat.max_tool_rounds",
    "chat.max_tool_rounds_hint",
    "chat.error.max_tool_rounds",
    "chat.storage_error.title",
    "chat.storage_error.quota",
    "chat.storage_error.corrupt",
    "chat.storage_error.serialization",
    "chat.storage_error.unavailable",
    "chat.storage_error.no_data_deleted",
    "chat.storage_error.download_pending",
    "chat.storage_error.download_raw",
    "chat.storage_error.retry",
    "chat.storage_error.manage",
}


def _template() -> str:
    return CHAT_TEMPLATE.read_text()


def _section(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_regeneration_overrides_survive_stream_context():
    stream = _section(
        _template(),
        "async streamResponse(streamContext = null, depth = 0)",
        "stopStreaming()",
    )

    assert "_modelOverride: streamContext?._modelOverride ?? null" in stream
    assert "_generationOverride: streamContext?._generationOverride ?? null" in stream
    assert "context._modelOverride || context.model" in stream
    assert "context._generationOverride" in stream


def test_one_off_regeneration_does_not_replace_session_model():
    html = _template()
    stream = _section(
        html,
        "async streamResponse(streamContext = null, depth = 0)",
        "stopStreaming()",
    )
    regenerate = _section(
        html,
        "async regenerateMessage(index, opts = {})",
        "_copyFallback(text)",
    )

    assert "if (!context._modelOverride)" in stream
    assert "model: this.currentModel" in regenerate
    assert "_modelOverride: opts.model || null" in regenerate
    assert "chatSession.messages, context.model" not in stream
    assert "chatSession.messages, chatSession.model" in stream


def test_wheel_listener_is_registered_only_during_scroll_setup():
    html = _template()
    setup = _section(html, "    setupScrollListener() {", "    async downloadChats()")
    scroll = _section(
        html,
        "    scrollToBottom(force = false) {",
        "    forceScrollToBottom() {",
    )

    assert "addEventListener('wheel'" in setup
    assert "addEventListener('wheel'" not in scroll


def test_chat_history_is_sorted_and_committed_without_automatic_trimming():
    html = _template()
    save = _section(
        html,
        "    async saveCurrentChat(",
        "    startRenamingChat(chat)",
    )

    assert "const nextHistory = [...baseHistory]" in save
    assert save.index("this.sortChatHistory(nextHistory)") < save.index(
        "this.saveChatRecord(chatData"
    )
    # The hot path commits a single record; it must not rewrite the whole corpus.
    assert "this.saveChatHistory(nextHistory" not in save
    assert "MAX_CHAT_HISTORY_SIZE" not in html
    assert "chatHistory.pop()" not in html


def test_chat_history_failures_are_visible_and_preserve_recovery_data():
    html = _template()
    save = _section(html, "    async saveChatHistory(", "    async saveChatRecord(")
    record = _section(html, "    async saveChatRecord(", "    async startNewChat(")
    load = _section(html, "    async loadChatHistory(", "    async saveChatHistory(")
    failure = _section(html, "    handleRecordWriteFailure(", "    _downloadChatData(")

    # A failed whole-history save keeps the candidate in memory and reports it.
    assert "await store.put(chat" in save
    assert "this._pendingChatHistory = nextHistory" in save
    assert "pendingJson: JSON.stringify(nextHistory)" in save
    assert "this.chatHistory = nextHistory" in save
    assert save.index("if (failures.length)") < save.index("this.chatHistory = nextHistory")

    # A failed single-record save never discards the candidate either.
    assert "this.handleRecordWriteFailure(chat.id, put, chat)" in record
    assert "this.chatStorageIssue = null" in record

    # Bytes still on disk are handed back for backup, and a refused concurrent
    # write is surfaced as a conflict rather than silently clobbering.
    assert "raw: result.raw ?? null" in failure
    assert "kind: 'conflict'" in failure

    assert "this.chatStorageIssue" in load
    assert "chat.storage_error.no_data_deleted" in html
    assert "downloadPendingChatHistory()" in html
    assert "retryChatHistorySave()" in html
    assert ':inert="!!chatStorageIssue"' in html


def test_retry_commits_the_pending_candidate_and_rehydrates_sessions():
    section = _section(
        _template(),
        "    async retryChatHistorySave()",
        "    async importChats(event)",
    )

    assert "pending = this._pendingChatHistory" in section
    assert "this.saveChatHistory(pending" in section
    assert "await this.resyncChatHistoryState(currentId)" in section
    assert "this.chatSessions = {}" in section


def test_send_stops_before_inference_when_the_user_turn_cannot_be_saved():
    section = _section(
        _template(),
        "    async sendMessage()",
        "    async sendTranscriptionMessage()",
    )

    # The user turn is persisted *and awaited* before inference starts, so a
    # failed save cannot leave a streamed reply with nothing committed behind it.
    save_guard = "if (!await this.saveCurrentChat(chatId, chatSession.messages"
    assert save_guard in section
    assert section.index(save_guard) < section.index("await this.streamResponse({")


def test_import_is_all_or_nothing_and_has_no_count_cap():
    section = _section(_template(), "    async importChats(event)", "    async saveApiKey()")

    assert "const nextHistory" in section
    assert "if (!await this.saveChatHistory(nextHistory" in section
    assert ".slice(0," not in section


def test_chat_navigation_preserves_the_previous_chat_timestamp():
    html = _template()
    start_new = _section(
        html, "    async startNewChat(options = {})", "    async loadChat(chatId)"
    )
    load = _section(
        html,
        "    async loadChat(chatId)",
        "    async saveCurrentChat(",
    )
    save = _section(
        html,
        "    async saveCurrentChat(",
        "    startRenamingChat(chat)",
    )

    assert "{ touchUpdatedAt: false }" in start_new
    assert "{ touchUpdatedAt: false }" in load
    assert "options.touchUpdatedAt === false && existingChat?.updatedAt" in save
    assert "? existingChat.updatedAt" in save


def test_lazy_chat_creation_preserves_preconfigured_draft():
    html = _template()
    start_new = _section(
        html, "    async startNewChat(options = {})", "    async loadChat(chatId)"
    )
    send = _section(
        html,
        "    async sendMessage()",
        "    async sendTranscriptionMessage()",
    )
    transcription = _section(
        html,
        "    async sendTranscriptionMessage()",
        "    async streamTranscription(",
    )
    microphone = _section(
        html,
        "    async startMicTranscription()",
        "    stopMicTranscription(",
    )
    clear_all = _section(
        html,
        "    async clearAllHistory()",
        "    // Thinking/Reasoning tag processing",
    )

    assert (
        "const preserveDraft = options.preserveDraft === true && !prevChatId"
        in start_new
    )
    assert "? draftSystemPrompt" in start_new
    assert "? draftActiveProfile" in start_new
    assert "if (preserveDraft && this.modelSettingsDirty)" in start_new
    assert "this.syncSessionModelSettingsFromUi(session)" in start_new
    assert "this.loadModelCapabilities(" in start_new
    assert "this.resolveGatewayModelId(this.currentModel)" in start_new
    assert (
        "await this.ensureSessionModelSettings(session, this.currentModel)" in start_new
    )

    lazy_create = "await this.startNewChat({ preserveDraft: true })"
    assert lazy_create in send
    assert lazy_create in transcription
    assert lazy_create in microphone
    assert '<button @click="startNewChat()"' in html
    assert "await this.startNewChat();" in clear_all


def test_new_chat_strings_exist_in_every_locale():
    for locale_path in I18N_DIR.glob("*.json"):
        translations = json.loads(locale_path.read_text())
        missing = NEW_I18N_KEYS - translations.keys()
        assert not missing, f"{locale_path.name} is missing {sorted(missing)}"
        assert "{max}" in translations["chat.error.image_too_large"]


def test_tailwind_contains_new_chat_ui_utilities():
    css = TAILWIND_CSS.read_text()

    assert ".max-h-40{" in css
    assert ".z-\\[200\\]{" in css


def test_empty_thinking_content_is_not_rendered_or_replayed():
    html = _template()
    helper = _section(
        html,
        "            hasVisibleThinking(thinking) {",
        "            snapshotGenerationSettings()",
    )
    message_builder = _section(
        html,
        "            buildMessagesForApi(messages, systemPrompt, opts = {})",
        "            buildChatCompletionBody(messages, context, depth)",
    )
    renderer = _section(
        html,
        "    extractThinking(text) {",
        "    // Efficiently update streaming DOM",
    )
    stream = _section(
        html,
        "async streamResponse(streamContext = null, depth = 0)",
        "stopStreaming()",
    )

    assert "thinking.trim().length > 0" in helper
    assert "this.hasVisibleThinking(thinking) ? thinking : null" in helper
    assert "this.hasVisibleThinking(msg.reasoning_content)" in message_builder
    assert "this.hasVisibleThinking(msg._thinking)" in message_builder
    assert "if (content)" in renderer
    assert "if (!this.hasVisibleThinking(content)) return '';" in renderer
    assert "if (this.hasVisibleThinking(thinkingContent))" in renderer
    assert "&& this.hasVisibleThinking(stream.streamingThinking)" in stream
    assert "reasoning_content: this.hasVisibleThinking(stream.streamingThinking)" in stream
    assert 'x-if="hasVisibleThinking(msg._thinking)"' in html
    assert 'x-show="hasVisibleThinking(currentStream()?.streamingThinking)"' in html
