"""
Источник: откуда берётся список каналов.

Главное здесь — имя канала это ещё и ключ дедупликации в seen_posts. Разъедется
форма записи ('qa_jobs' vs '@qa_jobs') — вся история канала станет «новой» и
уедет в ИИ повторно.
"""

from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, Chat, User

from src.sources import telegram as telegram_module
from src.sources.telegram import normalize_channel, subscribed_channels


def _dialog(entity, name="диалог"):
    return SimpleNamespace(entity=entity, name=name)


class _FakeClient:
    """Клиент отдаёт заранее заданные диалоги — сети здесь нет."""

    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def iter_dialogs(self):
        for dialog in self._dialogs:
            yield dialog


def _channel(username, *, megagroup=False, broadcast=False):
    return Channel(
        id=1,
        title="канал",
        photo=None,
        date=None,
        username=username,
        megagroup=megagroup,
        broadcast=broadcast,
    )


# ---------- нормализация ----------


@pytest.mark.parametrize("value", ["qa_jobs", "@qa_jobs", "  @qa_jobs  ", " qa_jobs "])
def test_normalize_gives_one_form(value):
    """В БД ключ с собакой — к нему всё и приводим."""
    assert normalize_channel(value) == "@qa_jobs"


def test_env_channels_are_normalized(monkeypatch):
    """Забыть @ в .env не должно означать переоценку всей истории канала."""
    monkeypatch.setenv("CHANNELS", "qa_jobs, @other_jobs")
    import importlib

    from src.sources import telegram

    importlib.reload(telegram)
    try:
        assert telegram.CHANNELS == ["@qa_jobs", "@other_jobs"]
    finally:
        monkeypatch.delenv("CHANNELS")
        importlib.reload(telegram)


# ---------- автоподхват подписок ----------


async def test_megagroup_is_taken():
    """
    @qa_jobs — мегагруппа, а не broadcast. Фильтр «только broadcast» отсёк бы
    единственный источник, поэтому проверяем явно.
    """
    client = _FakeClient([_dialog(_channel("qa_jobs", megagroup=True))])

    assert await subscribed_channels(client) == ["@qa_jobs"]


async def test_broadcast_channel_is_taken():
    client = _FakeClient([_dialog(_channel("vacancies", broadcast=True))])

    assert await subscribed_channels(client) == ["@vacancies"]


async def test_people_and_groups_are_ignored():
    """Переписка с человеком и обычная группа — не источники вакансий."""
    client = _FakeClient(
        [
            _dialog(User(id=2), "Telegram"),
            _dialog(Chat(id=3, title="группа", photo=None, participants_count=2, date=None, version=1)),
            _dialog(_channel("qa_jobs", megagroup=True)),
        ]
    )

    assert await subscribed_channels(client) == ["@qa_jobs"]


async def test_private_channel_is_skipped_loudly(capsys):
    """Без username не собрать ни ссылку, ни ключ дедупа. Но не молча."""
    client = _FakeClient([_dialog(_channel(None, broadcast=True), "Приватный")])

    assert await subscribed_channels(client) == []
    assert "Приватный" in capsys.readouterr().out


# ---------- секреты нужны сети, а не импорту ----------


def test_import_does_not_require_secrets():
    """
    Пойман на деплое: раньше `API_ID = int(os.environ["API_ID"])` стоял на
    уровне модуля, и любой импорт требовал секретов. На свежем клоне тесты
    из-за этого не собирались вовсе — KeyError падал на сборе, до единой
    проверки. Локально не видно: .env лежит рядом и всё молча работает.
    """
    import importlib

    from src.sources import telegram

    importlib.reload(telegram)  # не должно бросить даже без .env


def test_make_client_explains_missing_credentials(monkeypatch):
    """Без ключей — понятная ошибка, а не голый KeyError из недр."""
    monkeypatch.delenv("API_ID", raising=False)
    monkeypatch.delenv("API_HASH", raising=False)

    with pytest.raises(RuntimeError, match="API_ID"):
        telegram_module.make_client()


# ---------- приоритет: аргумент -> .env -> подписки ----------


@pytest.fixture
def fake_telethon(monkeypatch):
    """
    Подменяем ФАБРИКУ клиента, а не сам TelegramClient.

    make_client читает креды из .env — подменив только TelegramClient, тест
    всё равно требовал бы секретов и падал на чистой машине. Фабрика для того
    и заведена: секреты нужны тому, кто идёт в сеть, а не тесту.
    """
    read_from: list[str] = []

    class _Client(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__([_dialog(_channel("from_subscription", megagroup=True))])

        async def start(self):
            return self

        async def disconnect(self):
            return None

        async def iter_messages(self, channel, limit=None):
            read_from.append(channel)
            return
            yield  # делает метод асинхронным генератором

    monkeypatch.setattr(telegram_module, "make_client", lambda: _Client())
    return read_from


async def test_explicit_argument_wins(fake_telethon, monkeypatch):
    monkeypatch.setattr(telegram_module, "CHANNELS", ["@from_env"])

    async for _ in telegram_module.iter_posts(10, channels=["@explicit"]):
        pass

    assert fake_telethon == ["@explicit"]


async def test_env_wins_over_subscriptions(fake_telethon, monkeypatch):
    monkeypatch.setattr(telegram_module, "CHANNELS", ["@from_env"])

    async for _ in telegram_module.iter_posts(10):
        pass

    assert fake_telethon == ["@from_env"]


async def test_subscriptions_used_when_env_empty(fake_telethon, monkeypatch):
    """Ради этого всё и делалось: пустой .env — читаем подписки техаккаунта."""
    monkeypatch.setattr(telegram_module, "CHANNELS", [])

    async for _ in telegram_module.iter_posts(10):
        pass

    assert fake_telethon == ["@from_subscription"]
