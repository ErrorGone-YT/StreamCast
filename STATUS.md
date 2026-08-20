# StreamCast — статус проекта

## Запуск

```bash
cd /c/StreamCast
./venv/Scripts/python.exe app.py
```

Открыть: **http://127.0.0.1:5000** (пароль отключён, дашборд сразу)

---

## Что готово и проверено

### Ядро
- Загрузка видео → нормализация в фоне (1080p / 30fps / AAC) через `encoder.py`
- Блок 60fps — файлы отклоняются с ошибкой
- Один ffmpeg-процесс на стрим, `-c copy`, бесконечный цикл через concat
- Watchdog — автоперезапуск при сбое сети / дропе YouTube
- Запуск 5–10 стримов одновременно (проверено на 10)

### Очередь
- Drag-n-drop порядка воспроизведения
- **Горячий подхват изменений** без остановки эфира (на границе блока, ~15 мин)
- Кнопка **«↻ Apply queue now»** — мгновенная пересборка без Stop
- Индикатор **«● ON AIR»** + прогресс-бар текущего ролика (обновляется каждые 3 сек)
- Статусы кодирования в реальном времени: `waiting_encode → encoding → completed / error`

### Интерфейс
- Динамическая белая тема: aurora-фон, glass topbar, анимированные карточки
- Шрифт Inter (Google Fonts)
- Dropzone для загрузки (drag или клик)

### Прочее
- Планировщик автостарта по дате/времени
- Режим «без цикла» — один проход и стоп
- README с гайдом по деплою на VPS (Ubuntu + gunicorn + systemd + nginx + HTTPS)

---

## Что НЕ проверялось вживую

| Что | Что нужно |
|---|---|
| **Реальный эфир на YouTube** | Stream key из YouTube Studio (приватный стрим для теста) |
| Watchdog в бою | Обрыв сети / дроп YouTube во время живого эфира |
| Автостарт по расписанию | Живой стрим |
| Деплой на VPS | Сервер Ubuntu |

---

## Файловая структура

```
C:\StreamCast\
├── app.py          — Flask: роуты, логин, upload, start/stop/apply
├── config.py       — все настройки через env-переменные
├── db.py           — SQLite: таблицы streams + videos
├── encoder.py      — фоновый воркер нормализации видео
├── streamer.py     — StreamManager: ffmpeg-процессы, hot-reload, now-playing
├── requirements.txt
├── .env.example
├── README.md       — гайд по установке
├── STATUS.md       — этот файл
├── static/
│   └── style.css   — динамическая белая тема
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── dashboard.html
│   ├── stream.html
│   └── stream_edit.html
├── storage/        — uploads/ + encoded/ + streamcast.db (создаётся автоматически)
└── venv/           — виртуальное окружение Python
```

---

## Ключевые env-переменные

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `STREAMCAST_PASSWORD` | `changeme` | Пароль входа |
| `STREAMCAST_REQUIRE_LOGIN` | `0` | `1` — включить пароль |
| `STREAMCAST_RTMP_BASE` | YouTube live2 | База RTMP |
| `STREAMCAST_STORAGE` | `./storage` | Папка данных |
| `STREAMCAST_WIDTH/HEIGHT` | `1920/1080` | Разрешение |
| `STREAMCAST_FPS` | `30` | FPS |
| `STREAMCAST_VBITRATE` | `4500k` | Битрейт видео |
| `STREAMCAST_RELOAD_BLOCK_SECONDS` | `900` | Как часто подхватывать изменения очереди (сек) |

---

## Следующий шаг — первый реальный эфир

1. YouTube Studio → **Создать → Прямой эфир → Потоковое вещание**
2. Скопировать **Ключ потока** (вид: `xxxx-xxxx-xxxx-xxxx`)
3. В StreamCast: **+ New stream** → вставить ключ → загрузить несколько видео → **● Go live**
4. На YouTube проверить, что пошла картинка (задержка ~10–20 сек)
