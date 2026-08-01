Готовые образы: https://hub.docker.com/r/lordarrin/tg-ws-proxy

Образ Docker для личного использования. Все права принадлежат оригинальному автору: https://github.com/Flowseal/tg-ws-proxy

Я просто немного изменил синтаксис:

| Аргументы | Значение по умолчанию | Описание |
|---|---|---|
| `PROXY_PORT` | `1443` | Порт прокси |
| `PROXY_HOST` | `0.0.0.0` | Хост прокси |
| `PROXY_SECRET` | `random` | 32 hex chars secret для авторизации клиентов. (Команда для генерации - `openssl rand -hex 16`) |
| `PROXY_DC_IPS` | `2:149.154.167.220 4:149.154.167.220` | Целевой IP для DC (можно указать несколько раз) |
| `PROXY_BUF` | `4096` | Размер буфера в КБ |
| `PROXY_POOL_SIZE` | `2` | Количество заготовленных соединений на каждый DC |
| `NO_CFPROXY` | `false` | Отключить попытку [проксирования через Cloudflare](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfProxy.md) |
| `CFPROXY_DOMAIN` | | Указать свой [домен](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfProxy.md) для проксирования через Cloudflare. |
| `CFPROXY_WORKER_DOMAIN` | | Указать свой [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfWorker.md) для проксирования. Можно указать несколько доменов, разделив их пробелом (например: `worker1.dev worker2.dev`). |
| `KEEPALIVE` | `false` | Включить WS keepalive пинги для предотвращения закрытия соединений при простое. Экспериментальная опция, отключена по умолчанию. Для работы требуется настроить [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfWorker.md). |
| `SOCKS_ENABLED` | `false` | Включить встроенный SOCKS5 прокси с туннелированием трафика через Cloudflare Worker. |
| `SOCKS_PORT` | `1080` | Порт для прослушивания SOCKS5 прокси. |
| `SOCKS_HOST` | `0.0.0.0` | Хост для прослушивания SOCKS5 прокси. |
| `SOCKS_USER` | | *(Опционально)* Имя пользователя для аутентификации SOCKS5. Если указано вместе с `SOCKS_PASS`, аутентификация становится строго обязательной. |
| `SOCKS_PASS` | | *(Опционально)* Пароль для аутентификации SOCKS5. |
| `SOCKS_CONNECT_TIMEOUT` | `10` | Таймаут установки WebSocket-соединения с Cloudflare Worker (в секундах). |