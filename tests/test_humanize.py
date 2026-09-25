from app.humanize import send_humanized, split_messages, typing_delay


def test_split_by_paragraph():
    assert split_messages("Olá.\n\nTudo bem?") == ["Olá.", "Tudo bem?"]


def test_split_limits_to_three_parts():
    parts = split_messages("a\n\nb\n\nc\n\nd\n\ne")
    assert len(parts) == 3
    assert parts[:2] == ["a", "b"]
    assert parts[2] == "c\n\nd\n\ne"


def test_split_ignores_empty():
    assert split_messages("  \n\n  ") == []


def test_typing_delay_formula():
    assert typing_delay("") == 1.5
    assert typing_delay("x" * 40) == 2.5
    assert typing_delay("x" * 1000) == 6.0


async def test_send_humanized_waits_and_sends_in_order():
    sent, waits, typing = [], [], []

    async def send(cid, text):
        sent.append((cid, text))

    async def fake_typing(cid, on):
        typing.append(on)

    async def sleep(seconds):
        waits.append(seconds)

    await send_humanized(7, "Primeira.\n\nSegunda mensagem.", send, fake_typing, sleep)
    assert sent == [(7, "Primeira."), (7, "Segunda mensagem.")]
    assert waits == [typing_delay("Primeira."), typing_delay("Segunda mensagem.")]
    assert typing[-1] is False
