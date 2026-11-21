# API агента узла

Контракт между агентом (`agent/savebit_agent.py`) и бэкендом `api.savebit.ru`.
Тестовая реализация: `tools/mock_api.py`.

Базовый путь: `https://api.savebit.ru/api/v1/agent`. Только HTTPS (HTTP допустим для тестов).

## Авторизация: ключ узла

Каждый запрос агента несёт заголовок:

```
Authorization: Bearer sbn_7Qm2...
```

- Ключ выдаётся в админке при создании узла и показывается **один раз**. Предлагаемый формат: `sbn_` + 32 случайных символа (`secrets.token_urlsafe(24)`).
- В базе храним только `sha256(ключ)`. Поиск узла по хешу.
- «Перевыпустить ключ»: новый хеш записывается вместо старого, старый ключ сразу перестаёт работать.
- Ключ открывает только конфиг своего узла и запись отчёта о нём. Никаких других прав у него нет.
- Неверный или отозванный ключ: `401`. Агент продолжает работать на последнем конфиге и пишет в журнал подсказку про `savebit-agent rekey`.

## GET /config

Агент спрашивает раз в `interval` секунд (по умолчанию 60, допустимо 15–3600).

Запрос:

```
GET /api/v1/agent/config
Authorization: Bearer <ключ>
If-None-Match: "<ревизия, которая уже применена>"
```

Если конфиг не изменился: `304 Not Modified`, без тела.

Иначе `200` и `ETag: "<ревизия>"`:

```json
{
  "node": { "id": "uuid", "name": "in-msk-1", "role": "entry" },
  "revision": "8f882a40a0a9",
  "interval": 60,
  "xray": {
    "version": "26.11.9",
    "url": null,
    "sha256": null
  },
  "tls": [
    { "name": "hy2", "cn": "www.example.com" }
  ],
  "config": { "...": "полный конфиг Xray, см. ниже" }
}
```

| Поле | Обязательно | Смысл |
|---|---|---|
| `node.role` | да | `entry` или `exit` |
| `revision` | да | хеш всего ответа без самого `revision`. Меняется при любом изменении: друзья, выходы, версия Xray |
| `interval` | нет | как часто агенту приходить, секунды |
| `xray.version` | да | закреплённая версия, например `26.11.9`. Агент сам поставит её и переключится |
| `xray.url` | нет | откуда качать архив вместо GitHub, например зеркало на api.savebit.ru (запрос пойдёт с тем же ключом) |
| `xray.sha256` | нет | контрольная сумма архива. Без `url` агент сверяет с `.dgst` из релиза на GitHub |
| `tls[]` | нет | какие самоподписанные сертификаты создать: `/etc/savebit/tls/<name>.crt` и `.key`. Создаются один раз и не пересоздаются |
| `config` | да | конфиг Xray целиком |

### Правила для `config`

- В путях к сертификатам пиши `${TLS_DIR}`: агент подставит настоящий каталог. Пример: `"certificateFile": "${TLS_DIR}/hy2.crt"`.
- Не указывай файлы логов (`log.access`, `log.error`): Xray пишет в journald (`journalctl -u savebit-xray`).
- Для статистики в отчёте нужен API Xray на `127.0.0.1`, например `127.0.0.1:10085`, со службами `HandlerService` и `StatsService` (на входе ещё `RoutingService`, `ObservatoryService`), а также `stats` и `policy` с `statsUserUplink`, `statsUserDownlink`, `statsUserOnline`.
- `email` клиента = идентификатор друга в бэкенде (например, его id). По нему считается трафик и онлайн.
- Теги, которые ожидает отчёт и мониторинг:
  - входящие на входе: `in-reality`, `in-xhttp`, `in-hy2`;
  - исходящие на входе: `exit-<имя-выхода>-xhttp`, `exit-<имя-выхода>-hy2`;
  - балансировщик на входе: `exits`, селектор `exit-`, стратегия `leastPing`, `observatory` с `subjectSelector: ["exit-"]`.
- Xray 26.x убрал `allowInsecure`. Самоподписанные сертификаты Hysteria2 проверяются только по отпечатку: `tlsSettings.pinnedPeerCertSha256`. Отпечаток приходит от агента в отчёте (см. ниже).
- Исходящий `freedom` в Xray 26.x по умолчанию блокирует частные адреса (127.0.0.1, 10.x и т. п.). Это и нужно.

Эталоны: `examples/entry.json` и `examples/exit.json`.

### Что бэкенд кладёт в конфиг

**Выход** (`role: exit`):
- `in-xhttp`: VLESS + XHTTP + REALITY на 443/tcp. Клиенты: служебные пользователи входов (по одному на вход) и все друзья (для прямого профиля).
- `in-hy2`: Hysteria2 + Salamander на 443/udp. Клиенты: служебные пользователи входов.
- Исходящие: `direct` (freedom) и `block`.
- REALITY выхода маскируется под сайт, доступный из страны выхода (не `max.ru`: он закрыт для многих зарубежных адресов).

**Вход** (`role: entry`):
- `in-reality`: VLESS + REALITY + Vision на 443/tcp, `target` = российский сайт из белых списков, доступный из этого дата-центра.
- `in-xhttp`: VLESS + XHTTP + REALITY на 8443/tcp.
- `in-hy2`: Hysteria2 + Salamander на 443/udp и диапазоне портов для их смены, сертификат `hy2`.
- Клиенты всех трёх: все активные друзья.
- Исходящие: по два на каждый живой выход (`exit-…-xhttp`, `exit-…-hy2`), `direct`, `block`.
- В `exit-…-hy2`: `pinnedPeerCertSha256` = отпечаток сертификата `hy2` этого выхода из его последнего отчёта,
  `finalmask.quicParams`: `{"keepAlivePeriod": 5, "maxIdleTimeout": 10}`, чтобы мёртвый канал замечался быстрее.
- Балансировщик `exits` и `observatory` с `probeInterval` 10s.

Порядок: выход нужно поднять раньше входа. Пока от выхода не пришёл первый отчёт с отпечатком сертификата,
его Hysteria2-канал в конфиг входа не включаем (XHTTP-канал можно включать сразу).

## POST /report

После каждой синхронизации, то есть раз в `interval`. Ответ: `200 {"ok": true}`.

```json
{
  "agent_version": "0.1.0",
  "revision": "8f882a40a0a9",
  "applied_at": 1795268178,
  "error": null,
  "xray": { "version": "26.11.9", "active": true },
  "tls": { "hy2": { "sha256": "F2:88:B6:...:36", "cn": "www.example.com" } },
  "ports": ["443/tcp", "443/udp", "8443/tcp", "20000-20050/udp"],
  "probes": [
    { "tag": "exit-de-xhttp", "address": "203.0.113.10", "port": 443, "proto": "tcp", "ok": true, "ms": 41.2 },
    { "tag": "exit-de-hy2", "address": "203.0.113.10", "port": 443, "proto": "udp", "ok": null, "ms": null }
  ],
  "balancers": { "exits": "exit-de-xhttp" },
  "traffic": {
    "users": { "friend-id-1": { "up": 104857, "down": 5242880 } },
    "online": 3
  },
  "host": {
    "hostname": "vm123", "os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.0", "cpus": 2,
    "uptime_sec": 86400, "load1": 0.12, "mem_used_pct": 31.5, "disk_used_pct": 22.0
  }
}
```

| Поле | Смысл |
|---|---|
| `revision` | какая ревизия сейчас применена. Не совпадает с актуальной и есть `error`: узел застрял на старом конфиге |
| `error` | текст последней ошибки (ключ, недоступный API, `xray -test`, откат) или `null` |
| `tls.*.sha256` | отпечаток сертификата в формате OpenSSL. Сохраняем: он нужен для `pinnedPeerCertSha256` на входах и для `pinSHA256` в ссылках Hysteria2 |
| `probes` | TCP-проверка каждого следующего узла с этого узла. Для UDP-каналов `ok: null`: их здоровье видно по `balancers` и по ping-service |
| `balancers` | какой исходящий сейчас выбрал балансировщик входа |
| `traffic.users` | трафик по `email` **с прошлого отчёта** (агент сбрасывает счётчики). Суммирует бэкенд |
| `traffic.online` | сколько друзей сейчас подключено |
| `access` | с агента 0.2.0. `null`, если в конфиге нет `log.access`. Иначе `{"users": {email: {"ips": {ip: n}, "domains": {домен: n}}}, "lines": N}` — сводка журнала соединений Xray **с прошлого отчёта**: с каких IP и к каким основным доменам (`rr3---sn-x.googlevideo.com` → `googlevideo.com`) подключался друг, число подключений. Служебные каналы `relay:…` не входят. До 40 IP и 150 доменов на друга |

Узел считается недоступным, если отчёта нет дольше трёх интервалов.

## Ссылки для подписки (для `/sub/{токен}`)

Бэкенд собирает их из тех же данных, что и конфиг входа. Перед запуском проверить на актуальных Happ для Android и iOS.

```
vless://<uuid>@in1.savebit.ru:443?type=tcp&security=reality&flow=xtls-rprx-vision&sni=<sni>&fp=chrome&pbk=<publicKey>&sid=<shortId>#Savebit · REALITY
vless://<uuid>@in1.savebit.ru:8443?type=xhttp&path=<path>&mode=auto&security=reality&sni=<sni>&fp=chrome&pbk=<publicKey>&sid=<shortId>#Savebit · XHTTP
hysteria2://<auth>@in1.savebit.ru:443?sni=<cn>&obfs=salamander&obfs-password=<пароль>&pinSHA256=<отпечаток>&mport=20000-20050#Savebit · Hysteria2
```

Публичный ключ REALITY для ссылок считается из приватного (`xray x25519 -i <privateKey>`), хранить можно оба.


## Выход не видит API напрямую

Бывает, что хостинг выхода и хостинг API режут друг друга (так у firstbyte и Timeweb). Тогда выход ходит в API через вход:

- на каждом входе в конфиге есть inbound `agent-relay` (dokodemo-door, TCP `agent_relay_port`, по умолчанию 9443), который пробрасывает TCP на `api.savebit.ru:443`;
- агент выхода подключается к `ВХОД:9443`, но TLS устанавливает с `api.savebit.ru`: SNI и проверка сертификата сквозные, вход видит только шифрованный поток;
- включается так: `install.sh --via ВХОД:9443` при установке или `savebit-agent set-via ВХОД:9443` потом; выключается `savebit-agent set-via off`. Хранится в `SAVEBIT_API_VIA` в `/etc/savebit/agent.env`.


## Журнал соединений (агент 0.2.0+)

Чтобы получать `access` в отчёте, бэкенд добавляет в конфиг узла:

```json
"log": {"loglevel": "warning", "access": "${LOG_DIR}/access.log"}
```

Агент подставляет `${LOG_DIR}` (`/var/lib/savebit/xray-log`), создаёт папку для пользователя `savebit`, после каждой
синхронизации дочитывает журнал с прошлой позиции и обрезает его, когда тот больше 4 МБ. Без `log.access` журнал удаляется.
Старым агентам (0.1.0) такой конфиг не отдавать: они не знают `${LOG_DIR}`, и Xray не запустится. Версия агента есть в каждом отчёте.
