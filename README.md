Готовые образы: https://hub.docker.com/r/lordarrin/tg-ws-proxy

Образ Docker для личного использования. Все права принадлежат оригинальному автору: https://github.com/Flowseal/tg-ws-proxy

Я просто немного изменил синтаксис:

| Аргументы | Значение по умолчанию | Описание |
|---|---|---|
| `PROXY_PORT` | `1443` | Порт прокси |
| `PROXY_HOST` | `0.0.0.0` | Хост прокси |
| `PROXY_SECRET` | `random` | 32 hex chars secret для авторизации клиентов |
| `PROXY_DC_IPS` | `2:149.154.167.220 4:149.154.167.220` | Целевой IP для DC (можно указать несколько раз) |
| `PROXY_BUF` | `2048` | Размер буфера в КБ |
| `PROXY_POOL_SIZE` | `2` | Количество заготовленных соединений на каждый DC |
| `NO_CFPROXY` | `false` | Отключить попытку [проксирования через Cloudflare](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfProxy.md) |
| `CFPROXY_DOMAIN` |    | Указать свой [домен](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfProxy.md) для проксирования через Cloudflare. Можно указать несколько через повторение аргумента. |
| `CFPROXY_WORKER_DOMAIN` |    | Указать свой [CF worker](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfWorker.md) для проксирования через Cloudflare. Можно указать несколько через повторение аргумента. |
