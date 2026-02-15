# ПахмаБот 🍺

Telegram-бот, который каждый понедельник в 9:00 МСК публикует опрос «Как пахма?» в канале друзей, а в 18:00 подводит итоги.

## Возможности

- Еженедельный опрос по шкале 0–10
- Случайные приветствия (в том числе сезонные)
- Пункт «Фантомная пахма» — когда не пил, но всё равно плохо
- Автоматическое закрытие и подсчёт среднего
- Сравнение с прошлой неделей и историческими рекордами
- Хранение всей истории голосований в JSON

## Быстрый старт (Docker)

### 1. Создать бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot`, задай имя и username
3. Скопируй токен (вида `123456789:AABBCCDDEEFFaabbccddeeff`)

### 2. Добавить бота в канал

1. Добавь бота **администратором** канала
2. Дай ему права: **отправка сообщений** и **управление опросами** (Send Messages, Manage Polls) — остальное необязательно
3. Узнай ID канала. Самый простой способ:
   - Перешли любое сообщение из канала в [@userinfobot](https://t.me/userinfobot) или [@getidsbot](https://t.me/getidsbot)
   - Или используй формат `@channel_username` (если канал публичный)

### 3. Настроить переменные окружения

```bash
cp .env.example .env
```

Отредактируй `.env`:

```
BOT_TOKEN=твой_токен_от_BotFather
CHANNEL_ID=-1001234567890
```

### 4. Запустить

```bash
docker compose up -d --build
```

Проверить логи:

```bash
docker compose logs -f pahmabot
```

### 5. Обновление

```bash
git pull
docker compose up -d --build
```

## Без Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# отредактировать .env
python bot.py
```

## Тестирование

Для быстрой проверки можно изменить расписание в `config.py`:

```python
POLL_HOUR = 12     # час МСК когда отправить опрос
CLOSE_HOUR = 12    # час МСК когда закрыть (поставь через 1 минуту от отправки)
WEEKDAY = 6        # 6 = воскресенье (если тестируешь в вск)
```

Или добавить в `bot.py` ручную отправку при старте:

```python
# В функции main(), после scheduler.start():
await send_poll(bot)
```

## Структура проекта

```
pahmabot/
├── bot.py              # Основная логика бота
├── config.py           # Настройки, тексты, варианты опроса
├── requirements.txt    # Python-зависимости
├── .env.example        # Шаблон переменных окружения
├── .env                # Твои переменные (не коммитится)
├── Dockerfile
├── docker-compose.yml
└── data/
    └── history.json    # История голосований (создаётся автоматически)
```

## Деплой на VPS — пошаговый план

### Требования

- VPS с Ubuntu/Debian (любой Linux)
- Docker и Docker Compose

### Шаги

```bash
# 1. Подключиться к серверу
ssh user@your-server-ip

# 2. Установить Docker (если ещё нет)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# перелогиниться чтобы группа применилась

# 3. Склонировать репозиторий
git clone https://github.com/YOUR_USERNAME/pahmabot.git
cd pahmabot

# 4. Создать .env
cp .env.example .env
nano .env   # вписать BOT_TOKEN и CHANNEL_ID

# 5. Запустить
docker compose up -d --build

# 6. Проверить что работает
docker compose logs -f

# 7. Бот будет автоматически перезапускаться при перезагрузке VPS
#    (благодаря restart: unless-stopped в docker-compose.yml)
```

### Обновление на сервере

```bash
cd pahmabot
git pull
docker compose up -d --build
```
