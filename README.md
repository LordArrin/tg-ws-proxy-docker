Готовые образы: [Docker Hub](https://hub.docker.com/r/lordarrin/tg-ws-proxy)

Образ Docker для личного использования. Все права принадлежат оригинальному [автору](https://github.com/Flowseal/tg-ws-proxy).

Я просто внёс несколько улучшений для собственного комфорта:

| Аргументы | Значение по умолчанию | Описание |
|---|---|---|
| `PROXY_PORT` | `1443` | Порт прокси |
| `PROXY_HOST` | `0.0.0.0` | Хост прокси |
| `PROXY_SECRET` | `random` | 32 hex chars secret для авторизации клиентов. (Команда для генерации - `openssl rand -hex 16`) |
| `PROXY_DC_IPS` | `2:149.154.167.220 4:149.154.167.220` | Целевой IP для DC (можно указать несколько раз) |
| `PROXY_BUF` | `4096` | Размер буфера в КБ |
| `PROXY_POOL_SIZE` | `2` | Количество заготовленных соединений на каждый DC |
| `NO_CFPROXY` | `false` | Отключить попытку [проксирования через Cloudflare](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/RU/CfProxy.md) |
| `NO_H2` | `false` | Отключить новый [экспериментальный режим проксирования медиа](https://github.com/Flowseal/tg-ws-proxy/discussions/1391) |
| `CFPROXY_DOMAIN` | | Указать свой [домен](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/RU/CfProxy.md) для проксирования через Cloudflare. |
| `CFPROXY_WORKER_DOMAIN` | | Указать свой [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/RU/CfWorker.md) для проксирования. Можно указать несколько доменов, разделив их пробелом (например: `worker1.dev worker2.dev`). |
| `KEEPALIVE` | `false` | Включить WS keepalive пинги для предотвращения закрытия соединений при простое. Экспериментальная опция, отключена по умолчанию. Для работы требуется настроить [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfWorker.md). |
| `SOCKS_ENABLED` | `false` | Включить встроенный SOCKS5 прокси с туннелированием трафика через [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfWorker.md). |
| `SOCKS_PORT` | `1080` | Порт для прослушивания SOCKS5 прокси. |
| `SOCKS_HOST` | `0.0.0.0` | Хост для прослушивания SOCKS5 прокси. |
| `SOCKS_USER` | | *(Опционально)* Имя пользователя для аутентификации SOCKS5. Если указано вместе с `SOCKS_PASS`, аутентификация становится строго обязательной. |
| `SOCKS_PASS` | | *(Опционально)* Пароль для аутентификации SOCKS5. |
| `SOCKS_CONNECT_BUDGET` | `3.5` | Бюджет установки WS-соединения с [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfWorker.md) (сек). |
| `SOCKS_DNS_TIMEOUT` | `3` | Таймаут DNS-резолва целевого хоста (сек). |
| `SOCKS_MAX_CONNS` | `6` | Максимум одновременных SOCKS5-туннелей. |
| `SOCKS_QUEUE_TIMEOUT` | `1.5` | Ожидание свободного слота при насыщении (сек); `0` — сразу отказ. |
| `SOCKS_IDLE_TIMEOUT` | `180` | Разрыв туннеля при простое обоих направлений (сек). |
| `SOCKS_PING_INTERVAL` | `30` | WS-ping при простое против закрытия соединения CF (сек); `0` — выкл. |
| `SOCKS_PREWARM_PER_TARGET` | `1` | Число прогретых WS на активный target; `0` — выкл. прогрев. |
| `SOCKS_PREWARM_INTERVAL` | `20` | Период добора прогретых соединений (сек). |
| `SOCKS_PREWARM_CONNECT_BUDGET` | `8` | Бюджет WS-handshake для фонового прогрева (сек). |
| `SOCKS_TARGET_TTL` | `300` | Греются только targets, использованные за это время (сек). |
| `SOCKS_POOL_MAX_AGE` | `90` | Максимальный срок жизни прогретого WS (сек). |
| `SOCKS_POOL_MAX_TOTAL` | `8` | Общий потолок прогретых WS. |
| `SOCKS_BUF_KB` | `256` | Размер буфера сокета SOCKS5 (КБ). |

> `SOCKS_CONNECT_TIMEOUT` из прежних версий заменён на `SOCKS_CONNECT_BUDGET`. Все новые параметры опциональны — без них поведение соответствует дефолтам.