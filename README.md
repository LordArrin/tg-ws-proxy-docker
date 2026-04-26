Готовые образы: https://hub.docker.com/r/lordarrin/tg-ws-proxy

Образ Docker для личного использования. Все права принадлежат оригинальному автору: https://github.com/Flowseal/tg-ws-proxy

Я просто немного изменил синтаксис:

| Аргументы | Значение по умолчанию | Описание |
|---|---|---|
| `PROXY_PORT` | `1443` | Порт прокси |
| `PROXY_HOST` | `0.0.0.0` | Хост прокси |
| `PROXY_SECRET` | `random` | 32 hex chars secret для авторизации клиентов |
| `PROXY_DC_IPS` | `2:149.154.167.220 4:149.154.167.220` | Целевой IP для DC (можно указать несколько раз) |
| `PROXY_BUF` | `1024` | Размер буфера в КБ |
| `PROXY_POOL_SIZE` | `8` | Количество заготовленных соединений на каждый DC |
| `NO_CFPROXY` | `false` | Отключить попытку [проксирования через Cloudflare]((https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfProxy.md)) |
| `CFPROXY_PRIORITY` | `true` | Пробовать проксировать через Cloudflare перед прямым TCP подключением |
| `CFPROXY_DOMAIN` |    | Указать свой домен для проксирования через Cloudflare. [Подробнее тут](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/CfProxy.md) |
