# SPDX-License-Identifier: Apache-2.0
"""Regression contract for browser-to-inference stream cancellation."""

import json
from pathlib import Path

CHAT_TEMPLATE = (
    Path(__file__).parents[1] / "omlx" / "admin" / "templates" / "chat.html"
)
I18N_DIR = Path(__file__).parents[1] / "omlx" / "admin" / "i18n"


def test_stop_cancels_the_open_response_reader_before_aborting_fetch():
    source = CHAT_TEMPLATE.read_text(encoding="utf-8")
    helper_start = source.index("cancelStreamTransport(stream)")
    helper_end = source.index("stopChatStreaming(chatId)", helper_start)
    helper = source[helper_start:helper_end]

    assert "reader.cancel('Stopped by user')" in helper
    assert helper.index("reader.cancel('Stopped by user')") < helper.index(
        "stream.abortController?.abort()"
    )


def test_stream_exposes_its_response_reader_to_every_stop_path():
    source = CHAT_TEMPLATE.read_text(encoding="utf-8")

    assert "stream.responseReader = reader;" in source
    assert "this.cancelStreamTransport(stream);" in source
    assert (
        "this.cancelStreamTransport(this.getStreamSession(chatId, false));" in source
    )


def test_accumulated_stream_helper_persists_received_output():
    source = CHAT_TEMPLATE.read_text(encoding="utf-8")
    helper_start = source.index("commitAccumulatedStream(")
    helper_end = source.index("cancelStreamTransport(stream)", helper_start)
    helper = source[helper_start:helper_end]

    assert "(stream._toolRoundContent || '')" in helper
    assert "+ (stream.streamingContent || '')" in helper
    assert "stream.streamingThinking" in helper
    assert "assistantMsg._interrupted = true" in helper
    assert "assistantMsg._interruptionReason" in helper
    assert "chatSession.messages.push(assistantMsg)" in helper
    assert "this.saveCurrentChat(" in helper


def test_unexpected_stream_failure_preserves_partial_before_using_error_message():
    source = CHAT_TEMPLATE.read_text(encoding="utf-8")
    stream_start = source.index("async streamResponse(")
    catch_start = source.index("} catch (error) {", stream_start)
    catch_end = source.index("} finally {", catch_start)
    catch = source[catch_start:catch_end]

    preserve = (
        "const partial = this.commitAccumulatedStream(\n"
        "                    context, chatSession, stream, requestModel, error\n"
        "                );"
    )
    assert preserve in catch
    assert "if (!partial)" in catch
    assert catch.index(preserve) < catch.index("if (!partial)")
    assert "content: `Error: ${error.message}`" in catch


def test_interrupted_badge_is_localized_in_every_locale():
    source = CHAT_TEMPLATE.read_text(encoding="utf-8")

    assert 'x-show="msg._interrupted"' in source
    assert "window.t('chat.generation_interrupted')" in source
    for locale_path in I18N_DIR.glob("*.json"):
        translations = json.loads(locale_path.read_text(encoding="utf-8"))
        assert "chat.generation_interrupted" in translations
