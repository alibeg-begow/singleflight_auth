# Proje Planı: `singleflight-auth` — httpx ve requests için Tekil-Uçuş Token Yenileme Kütüphanesi

> Bu doküman, projeyi sıfırdan PyPI'a kadar götürecek adım adım, uygulanabilir bir yol haritasıdır. Her bölüm önceki bölümün üzerine inşa edilir; sırayla ilerlemen önerilir.

---

## 0. Özet (TL;DR)

- **Ne yapıyoruz:** Python'da `httpx` (sync + async) ve `requests` için, bir HTTP isteği 401 aldığında jetonu **tek seferde** (eşzamanlı 10 istek 401 alsa bile refresh sadece 1 kez çalışır) yenileyip bekleyen tüm istekleri otomatik yeniden deneyen, çerçeveden bağımsız (framework-agnostic), hafif bir kütüphane yazıyoruz.
- **Kim için:** Kendi backend'ine JWT/özel token ile konuşan, OAuth2 spec'inin tamamına ihtiyacı olmayan, sadece "401 gelince yenile ve tekrar dene" mantığını her projede yeniden yazmaktan bıkmış Python geliştiricileri.
- **Neden farklı:** `httpx-auth` ve `requests_oauth2client` gibi büyük kütüphaneler tam OAuth2 istemcisi (authorization code, PKCE, client credentials vs.) — ağır ve opinionated. Biz OAuth2 flow'u implemente **etmiyoruz**; kullanıcı kendi `refresh()` fonksiyonunu veriyor, biz sadece eşzamanlılık/race-condition problemini çözüyoruz. Bu, "bring-your-own-refresh-logic" pozisyonu.
- **Bitiş noktası:** `pip install singleflight-auth` (veya seçtiğin isim) ile kurulabilen, testli, tip-güvenli, CI'lı, TestPyPI'da doğrulanmış, gerçek PyPI'da yayında bir paket + tanıtım planı.

---

## 1. Problemin Tam Tanımı

### 1.1 Somut senaryo

Bir SPA veya mobil uygulama, sayfa açılışında 6 farklı API çağrısını **paralel** olarak tetikler (dashboard verisi, kullanıcı profili, bildirimler, vs.). Access token'ın süresi tam o anda dolmuşsa:

1. 6 istek de sunucudan `401 Unauthorized` alır.
2. Naif bir istemci, her biri bağımsız şekilde refresh endpoint'ine istek atar → sunucuya 6 eşzamanlı refresh isteği gider.
3. Çoğu refresh-token implementasyonu **rotasyonlu**dur: bir refresh token kullanıldığında eski refresh token geçersiz kılınır ve yenisi verilir. 6 istek yarışırsa, ilk kazanan yeni refresh token'ı alır; geri kalan 5 istek **artık geçersiz olan eski refresh token'ı** kullanmaya çalışır ve bu da sunucudan hata döner.
4. Sonuç: kullanıcı oturumu düşer (zorla logout), ya da art arda "invalid_token" hataları, ya da bazı isteklerin sessizce başarısız olması.

Bu, teorik değil, gerçek bir GitLab OAuth2 issue'sunda da <cite index="54-1">bir refresh token kullanıldığında mevcut access_token ve refresh_token'ın geçersiz kılındığı ve bunun çok-thread'li/çok-process'li uygulamalarda yarış durumuna yol açtığı</cite> şeklinde belgelenmiş bir sınıf hatadır.

### 1.2 Neden her proje bunu yeniden yazıyor

`httpx`'in kendi dokümantasyonu bile bu deseni **paketlenmiş bir çözüm olarak değil**, kullanıcının kendi yazması gereken bir `Auth` alt sınıfı örneği olarak veriyor: <cite index="20-1">401 yanıtı geldiğinde jeton yenileme isteği gönderip jetonları güncelleyen ve orijinal isteği yeni jetonla tekrar gönderen özel bir kimlik doğrulama sınıfı</cite> yazman gerekiyor — ama bu örnekte **kilit/eşzamanlılık yok**. Aynı kütüphanenin GitHub tartışmasında bir kullanıcı tam olarak bunu arıyor ve <cite index="21-1">"cache stampede"i önleyecek bir eşzamanlılık primitifi olmadığını, şu an token süresini uzatarak spekülatif şekilde idare ettiğini</cite> yazmış — yani resmi kütüphane bile bunu "sizin sorununuz" olarak bırakıyor.

### 1.3 Sonuç

Bu, gerçek, tekrar eden, dokümante edilmiş bir problem. Ama bunu bir kütüphaneye dönüştürürken **kime yardım ettiğini net tanımlaman şart** — bir sonraki bölüm bunun neden önemli olduğunu gösteriyor.

---

## 2. Rekabet Analizi — Dürüst Bir Bakış

Kütüphaneyi yazmadan önce, "bu zaten var mı" sorusunu tam olarak cevaplamak lazım. Araştırdığım kadarıyla manzara şöyle:

| Kütüphane | Kapsam | Neden senin projenle aynı değil |
|---|---|---|
| `httpx` (resmi) | `Auth.auth_flow` deseni dokümante edilmiş | Kilit/queue **yok**, DIY bırakılmış |
| `httpx-auth` (Colin Bounouar) | Tam OAuth2 istemcisi (authorization code, client credentials, PKCE, token cache, tarayıcı entegrasyonu) — haftalık **541 bin+** indirme | <cite index="38-1">rfc6749'a göre OAuth2 flow'larının çoğunu implemente eden</cite>, ağır, spec'e bağlı bir kütüphane. Kendi refresh mantığın varsa (özel JWT endpoint'in gibi) uymuyor. |
| `requests_oauth2client` | `requests` için tam OAuth2 istemcisi | <cite index="47-1">Client Credentials, Authorization Code, Refresh Token, Token Exchange, Device Authorization gibi OAuth2.x/OIDC grant'lerini destekleyen</cite> ağır bir kütüphane; sen sadece "401 gelince benim fonksiyonumu çağır" istiyorsun. |
| `singleflight` (PyPI) | Genel amaçlı çağrı-birleştirme (Go'nun groupcache singleflight portu) | <cite index="43-1">HTTP'ye özel değil, herhangi bir fonksiyon çağrısını thread/gevent/asyncio ortamında tekilleştiren genel bir araç</cite> — HTTP/auth entegrasyonu, retry mantığı, 401 algılama yok. |
| `aiohttp` (resmi cookbook) | `asyncio.Lock` ile örnek middleware | <cite index="23-1">Sadece dokümantasyon örneği; paketlenmiş bir kütüphane değil</cite>, sadece aiohttp'ye özel. |
| Onlarca axios/fetch kütüphanesi (JS) | — | Python değil, konumuzun dışında. |

**Sonuç ve konumlama stratejisi:** Boşluk, "OAuth2 spec'ini tam implemente etmeyen, senin auth şeman ne olursa olsun (JWT, session cookie, custom header — hatta OAuth2 bile olabilir) sadece **eşzamanlılık problemini çözen, framework-agnostic (httpx + requests), minimal bağımlılıklı** bir kütüphane" konumunda. README'de bunu açıkça şöyle söyleyeceksin:

> "OAuth2 flow'larının tamamına ihtiyacın varsa `httpx-auth` veya `requests_oauth2client` kullan. Zaten kendi refresh mantığın var ve sadece paralel 401'lerin birbirini ezmesini önlemek istiyorsan, bu kütüphane senin için."

Bu dürüst konumlama, hem seni büyük kütüphanelerle kıyaslanmaktan hem de "zaten var" eleştirisinden korur.

---

## 3. Proje Kapsamı (Scope)

### 3.1 v0.1 (MVP) — Yapılacaklar

1. `httpx.Client` (sync) için `Auth` alt sınıfı — `threading.Lock` tabanlı.
2. `httpx.AsyncClient` (async) için `Auth` alt sınıfı — `asyncio.Lock` tabanlı, event loop'u **bloklamayan** tasarım.
3. `requests.Session` için `AuthBase` alt sınıfı — response hook tabanlı otomatik yeniden deneme.
4. "Tek uçuş" (single-flight) çekirdek algoritması: çakışan 401'lerde refresh fonksiyonunun **sadece bir kez** çağrılması.
5. Sonsuz döngü koruması (maksimum yeniden deneme sayısı, varsayılan 1).
6. Refresh fonksiyonu da başarısız olursa net, ayırt edilebilir bir `RefreshFailedError` fırlatılması.
7. Tip ipuçları (type hints) — `py.typed` marker dosyası ile.
8. %90+ test kapsamı, özellikle eşzamanlılık stres testleri.

### 3.2 v0.1'de Yapılmayacaklar (bilinçli olarak dışarıda bırakılıyor)

- OAuth2 grant flow'ları (authorization code, PKCE, client credentials) — kullanıcı kendi `refresh()` fonksiyonunda bunu yapar, kütüphane karışmaz.
- Token'ın nerede saklanacağı (disk, keyring, Redis, DB) — kullanıcı `get_token`/`save_token` callback'leri ile kendi yönetir.
- Süreçler-arası (multi-process) veya makineler-arası (dağıtık, Redis-lock gibi) senkronizasyon — v0.1 sadece **tek process içi** thread/task'lar arasını çözer. Bunu README'de açıkça belirt, aksi halde yanlış beklenti yaratırsın.
- `aiohttp` desteği — v1.0 sonrası backlog'a.
- Otomatik token süresi takibi (proaktif yenileme) — sadece **reaktif** (401 geldiğinde yenile) davranış v0.1 kapsamında.

Bu net sınırları çizmek, hem geliştirmeyi hızlandırır hem de README'de "ne yapmaz" bölümünü güçlü kılar (kullanıcı güveni için önemli).

---

## 4. Mimari Tasarım — Çekirdek Teknik Çözüm

Bu bölüm projenin kalbi; burayı iyi anlarsan kodu yazmak kolaylaşır.

### 4.1 Temel Algoritma: "Double-Checked Locking" ile Tek Uçuş

Amaç: N eşzamanlı istek aynı anda 401 alırsa, refresh fonksiyonu **tam olarak 1 kez** çalışsın, diğer N-1 istek kilidi bekleyip **hazır olan yeni token'ı** kullansın.

Yöntem, klasik "double-checked locking" desenidir:

```
1. İstek 401 aldı, kullandığı eski token = T_stale
2. Kilidi al (thread'ler için bloklar, coroutine'ler için event loop'u bloklamadan bekler)
3. Kilit alındıktan SONRA güncel token'ı tekrar oku = T_current
4. Eğer T_current != T_stale:
      → Demek ki bekleşirken başka biri zaten yeniledi. Refresh'i TEKRAR ÇAĞIRMA.
      → T_current'ı kullan.
   Değilse (T_current == T_stale):
      → Sen "kazanan" isteksin. refresh() fonksiyonunu SEN çağır.
      → Sonucu kaydet, döndür.
5. Kilidi bırak.
6. Yeni token ile isteği tekrar dene.
```

Bu deseni her seferinde manuel bir "Future paylaşma" mekanizması kurmadan, sade bir kilit + karşılaştırma ile elde ediyoruz. Kilidi bekleyen tüm coroutine/thread'ler, kilit serbest kalınca aynı "artık token değişmiş" durumunu görüp kısa yoldan çıkar.

### 4.2 Framework-Agnostic Çekirdek (`_core.py`)

Bu mantığı önce **hiçbir HTTP kütüphanesine bağımlı olmayan**, saf Python ile yaz. Böylece hem httpx hem requests adaptörleri bunu paylaşır, ve en kolay test edilebilecek katman burası olur.

```python
# src/singleflight_auth/_core.py
import threading
from typing import Callable, TypeVar

T = TypeVar("T")


class SyncCoordinator:
    """Senkron (thread tabanlı) dünya için tek-uçuş koordinatörü."""

    def __init__(self, get_token: Callable[[], T], refresh: Callable[[], T]) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._lock = threading.Lock()

    def resolve(self, stale_token: T) -> T:
        with self._lock:
            current = self._get_token()
            if current != stale_token:
                return current  # başka bir thread zaten yeniledi
            return self._refresh()  # kazanan bu thread; refresh'i biz yapıyoruz
```

```python
# src/singleflight_auth/_core.py (devamı)
import asyncio
from typing import Awaitable


class AsyncCoordinator:
    """Asenkron (asyncio) dünya için tek-uçuş koordinatörü."""

    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], Awaitable[T]],
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    async def resolve(self, stale_token: T) -> T:
        async with self._lock:
            current = self._get_token()
            if current != stale_token:
                return current
            return await self._refresh()
```

> **Kritik nokta:** `asyncio.Lock`, `threading.Lock`'tan farklı olarak event loop'u **bloklamaz** — kilidi bekleyen coroutine'ler diğer görevlere kontrolü bırakır. Bu yüzden async yolda asla `threading.Lock` kullanma; event loop'un tamamını dondurursun.

### 4.3 httpx Entegrasyonu — Neden `sync_auth_flow` / `async_auth_flow`'u Ayrı Ayrı Override Etmek Gerekiyor

`httpx.Auth` sınıfının varsayılan davranışı, hem sync hem async akışın **aynı** `auth_flow` generator'ını sürmesidir. Ama bu generator **düz bir Python generator'ı** — içinde `await` kullanamazsın. Bu, senin async kilidini (`asyncio.Lock`) doğru şekilde kullanamayacağın anlamına gelir çünkü:

- Eğer `auth_flow` içinde `threading.Lock().acquire()` (bloklayan) çağırırsan → async client'ta **tüm event loop'u dondurursun** (sadece o isteği değil, o anda çalışan her şeyi).
- Eğer hiç kilit kullanmazsan → zaten çözmeye çalıştığın race condition geri gelir.

**Çözüm:** `auth_flow`'u hiç kullanma; `sync_auth_flow` (düz `def`, `threading.Lock` ile) ve `async_auth_flow`'u (`async def`, `asyncio.Lock` ile) **ayrı ayrı** override et. httpx, `Client` için `sync_auth_flow`'u, `AsyncClient` için `async_auth_flow`'u zaten doğrudan çağırıyor — ikisini de override etmek tamamen desteklenen bir kullanım.

```python
# src/singleflight_auth/httpx_auth.py
from __future__ import annotations
import threading
import asyncio
from typing import Callable, Awaitable, Iterator, AsyncIterator

import httpx

from ._core import SyncCoordinator, AsyncCoordinator
from ._exceptions import RefreshFailedError, MaxRetriesExceededError


class SingleFlightAuth(httpx.Auth):
    """httpx.Client (senkron) için tek-uçuş token yenileme."""

    def __init__(
        self,
        get_token: Callable[[], str],
        refresh: Callable[[], str],
        is_unauthorized: Callable[[httpx.Response], bool] = lambda r: r.status_code == 401,
        max_retries: int = 1,
    ) -> None:
        self._get_token = get_token
        self._coordinator = SyncCoordinator(get_token, self._wrap_refresh(refresh))
        self._is_unauthorized = is_unauthorized
        self._max_retries = max_retries

    @staticmethod
    def _wrap_refresh(refresh: Callable[[], str]) -> Callable[[], str]:
        def _safe_refresh() -> str:
            try:
                return refresh()
            except Exception as exc:  # noqa: BLE001 — kasıtlı: her hatayı yakalayıp sarmalıyoruz
                raise RefreshFailedError("Token yenileme fonksiyonu hata fırlattı") from exc
        return _safe_refresh

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        retries = 0
        while self._is_unauthorized(response) and retries < self._max_retries:
            retries += 1
            new_token = self._coordinator.resolve(stale_token=token)
            token = new_token
            request.headers["Authorization"] = f"Bearer {new_token}"
            response = yield request

        if self._is_unauthorized(response) and retries >= self._max_retries:
            raise MaxRetriesExceededError(
                f"{self._max_retries} deneme sonrası hâlâ 401 alınıyor"
            )


class AsyncSingleFlightAuth(httpx.Auth):
    """httpx.AsyncClient için tek-uçuş token yenileme."""

    def __init__(
        self,
        get_token: Callable[[], str],
        refresh: Callable[[], Awaitable[str]],
        is_unauthorized: Callable[[httpx.Response], bool] = lambda r: r.status_code == 401,
        max_retries: int = 1,
    ) -> None:
        self._get_token = get_token
        self._coordinator = AsyncCoordinator(get_token, self._wrap_refresh(refresh))
        self._is_unauthorized = is_unauthorized
        self._max_retries = max_retries

    @staticmethod
    def _wrap_refresh(refresh: Callable[[], Awaitable[str]]) -> Callable[[], Awaitable[str]]:
        async def _safe_refresh() -> str:
            try:
                return await refresh()
            except Exception as exc:  # noqa: BLE001
                raise RefreshFailedError("Async token yenileme fonksiyonu hata fırlattı") from exc
        return _safe_refresh

    async def async_auth_flow(self, request: httpx.Request) -> AsyncIterator[httpx.Request]:
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        retries = 0
        while self._is_unauthorized(response) and retries < self._max_retries:
            retries += 1
            new_token = await self._coordinator.resolve(stale_token=token)
            token = new_token
            request.headers["Authorization"] = f"Bearer {new_token}"
            response = yield request

        if self._is_unauthorized(response) and retries >= self._max_retries:
            raise MaxRetriesExceededError(
                f"{self._max_retries} deneme sonrası hâlâ 401 alınıyor"
            )
```

Not: `is_unauthorized` fonksiyonunun içine `response` geldiğinde, henüz gövdesi (`.read()`) çağrılmamış olabilir; senin ihtiyacın sadece `status_code` olduğu için bu sorun değil. Eğer ileride "gövdedeki hata koduna göre de tetikle" gibi bir opsiyon eklemek istersen, `requires_response_body = True` sınıf değişkenini set edip gövdeyi okuman gerekir — bunu v0.2 için not al.

### 4.4 requests Entegrasyonu — Hook Tabanlı Yeniden Deneme

`requests` senkron çalışır ve interceptor kavramı yoktur; onun yerine **response hook** ve elle bağlantı üzerinden yeniden gönderme (`response.connection.send`) kullanılır. Bu, `requests` ekosisteminde reauth için standart/idiomatik yöntemdir.

```python
# src/singleflight_auth/requests_auth.py
from __future__ import annotations
from typing import Callable

import requests
import requests.auth

from ._core import SyncCoordinator
from ._exceptions import RefreshFailedError, MaxRetriesExceededError


class SingleFlightAuth(requests.auth.AuthBase):
    def __init__(
        self,
        get_token: Callable[[], str],
        refresh: Callable[[], str],
        is_unauthorized: Callable[[requests.Response], bool] = lambda r: r.status_code == 401,
        max_retries: int = 1,
    ) -> None:
        self._get_token = get_token
        self._coordinator = SyncCoordinator(get_token, self._wrap_refresh(refresh))
        self._is_unauthorized = is_unauthorized
        self._max_retries = max_retries

    @staticmethod
    def _wrap_refresh(refresh: Callable[[], str]) -> Callable[[], str]:
        def _safe_refresh() -> str:
            try:
                return refresh()
            except Exception as exc:  # noqa: BLE001
                raise RefreshFailedError("Token yenileme fonksiyonu hata fırlattı") from exc
        return _safe_refresh

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        # requests, PreparedRequest üzerine keyfi öznitelik eklemeye izin verir;
        # bunu "bu istek hangi token ile gönderildi" ve "kaç kez denendi" izlemek için kullanıyoruz.
        request._sf_token_used = token          # type: ignore[attr-defined]
        request._sf_retry_count = 0             # type: ignore[attr-defined]
        request.register_hook("response", self._on_response)
        return request

    def _on_response(self, response: requests.Response, **kwargs) -> requests.Response:
        if not self._is_unauthorized(response):
            return response

        retried = getattr(response.request, "_sf_retry_count", 0)
        if retried >= self._max_retries:
            raise MaxRetriesExceededError(
                f"{self._max_retries} deneme sonrası hâlâ 401 alınıyor"
            )

        stale_token = getattr(response.request, "_sf_token_used", None)
        new_token = self._coordinator.resolve(stale_token=stale_token)

        # Bağlantıyı serbest bırakmak için mevcut gövdeyi tüket
        response.content  # noqa: B018 — kasıtlı: connection pool'u serbest bırakmak için

        new_request = response.request.copy()
        new_request.headers["Authorization"] = f"Bearer {new_token}"
        new_request._sf_token_used = new_token          # type: ignore[attr-defined]
        new_request._sf_retry_count = retried + 1        # type: ignore[attr-defined]

        new_response = response.connection.send(new_request, **kwargs)
        new_response.history = [*response.history, response]
        return new_response
```

### 4.5 Sonsuz Döngü Koruması

İki katman koruma var:
1. `max_retries` (varsayılan **1**) — refresh sonrası hâlâ 401 alınıyorsa (yani refresh token'ın kendisi de geçersizse, ya da sunucu tarafında başka bir sorun varsa) sonsuz döngüye girmek yerine `MaxRetriesExceededError` fırlatılır.
2. Retry sayacı **isteğe özel** tutulur (`_sf_retry_count`, request nesnesi üzerinde), coordinator'a değil — böylece bir isteğin yeniden denemesi başka bir isteğin sayaçını etkilemez.

### 4.6 Refresh Fonksiyonu Başarısız Olursa

`refresh()` kullanıcı tarafından yazılan bir fonksiyon; ağ hatası, geçersiz refresh token, sunucu 500'ü gibi her şey olabilir. Kütüphane bunu yutmaz — `RefreshFailedError` olarak sarmalayıp yeniden fırlatır, böylece:
- Kullanıcı `try/except RefreshFailedError` ile "oturumu sonlandır, login sayfasına yönlendir" gibi bir aksiyon alabilir.
- Orijinal hata (`__cause__` zinciri ile) kaybolmaz, hata ayıklaması kolay kalır.

### 4.7 Nihai Kullanıcı Deneyimi (Hedef API)

Projenin başarı ölçütü: kullanıcının yazması gereken kod, orijinal problem tanımındaki "30-40 satır karmaşık kilit/kuyruk mantığı" yerine bu kadar kısa olmalı:

```python
import httpx
from singleflight_auth import SingleFlightAuth

def get_access_token() -> str:
    return token_store.get("access")

def refresh_access_token() -> str:
    resp = httpx.post(
        "https://api.example.com/auth/refresh",
        json={"refresh_token": token_store.get("refresh")},
    )
    resp.raise_for_status()
    data = resp.json()
    token_store.set("access", data["access_token"])
    return data["access_token"]

auth = SingleFlightAuth(get_token=get_access_token, refresh=refresh_access_token)
client = httpx.Client(auth=auth, base_url="https://api.example.com")

# Artık 50 paralel istek atsan bile refresh sadece 1 kez tetiklenir.
```

---

## 5. Proje Dosya Yapısı

```
singleflight-auth/
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
├── CONTRIBUTING.md
├── .gitignore
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── publish.yml
├── src/
│   └── singleflight_auth/
│       ├── __init__.py          # public API export'ları
│       ├── py.typed             # PEP 561 tip işaretçisi (boş dosya)
│       ├── _core.py             # SyncCoordinator, AsyncCoordinator
│       ├── _exceptions.py       # RefreshFailedError, MaxRetriesExceededError
│       ├── httpx_auth.py        # SingleFlightAuth, AsyncSingleFlightAuth
│       └── requests_auth.py     # SingleFlightAuth (requests.auth.AuthBase)
└── tests/
    ├── conftest.py
    ├── test_core.py             # saf mantık testleri, HTTP yok
    ├── test_httpx_sync.py
    ├── test_httpx_async.py
    ├── test_requests.py
    └── test_concurrency_stress.py  # projenin "kanıt" testi
```

`src/` layout kullanıyoruz (paket kökte değil `src/` altında) çünkü bu, testlerin yanlışlıkla yerel dizindeki kodu değil, **kurulu paketi** import etmesini garantiler — paketleme hatalarını erken yakalar.

---

## 6. Adım Adım Geliştirme Planı (Fazlar)

Her faz, bir öncekinin çalışır ve test edilmiş olmasını varsayar. Sırayı atlama.

### Faz 0 — Ortam Kurulumu

```bash
# uv kurulu değilse:
curl -LsSf https://astral.sh/uv/install.sh | sh

mkdir singleflight-auth && cd singleflight-auth
uv init --lib --package singleflight-auth
git init
```

`uv init --lib` sana zaten `src/` layout'lu, `pyproject.toml`'lu bir iskelet kurar. Bunu Bölüm 8'deki tam `pyproject.toml` ile değiştireceksin.

- [ ] GitHub'da boş bir repo aç (`singleflight-auth` veya seçtiğin isim).
- [ ] `git remote add origin ...`, ilk commit, push.
- [ ] `.gitignore` ekle (Python + `dist/` + `.venv/` standart şablonu).

### Faz 1 — Çekirdek Mantık (`_core.py`) + Saf Testler

Bölüm 4.2'deki `SyncCoordinator` ve `AsyncCoordinator`'ı yaz. Bu fazda **hiç httpx/requests import etme** — sadece saf Python.

`tests/test_core.py` içinde test etmen gerekenler:
- [ ] Tek bir çağrıda `refresh()` doğru çağrılıyor mu, dönen değer doğru mu.
- [ ] `stale_token != get_token()` durumunda `refresh()` **hiç çağrılmıyor** mu (double-check yolu).
- [ ] `ThreadPoolExecutor` ile 20 thread aynı anda `resolve()` çağırdığında `refresh()` **tam olarak 1 kez** çalışıyor mu (`unittest.mock.Mock` ile `call_count` kontrolü).
- [ ] Async tarafta `asyncio.gather` ile 20 coroutine aynı anda `resolve()` çağırdığında aynı garanti.

Bu faz bitmeden bir sonrakine geçme — projenin **tüm değeri** bu çekirdekte, geri kalanı sadece adaptör.

### Faz 2 — httpx Sync Entegrasyonu

- [ ] `httpx_auth.py` içine `SingleFlightAuth` sınıfını yaz (Bölüm 4.3).
- [ ] Test için `pytest-httpserver` kur (gerçek bir yerel HTTP sunucusu ayağa kaldırıp gerçek istekler atmanı sağlar — mock'lamaktan daha güvenilir bir entegrasyon testi verir).

```bash
uv add --dev pytest pytest-httpserver
```

- [ ] `tests/test_httpx_sync.py`: sahte bir sunucu kur, ilk çağrıda 401, `Authorization: Bearer fresh-token` header'ı geldiğinde 200 dönsün. Tek istekle mutlu yol testi yaz.
- [ ] `max_retries` aşıldığında `MaxRetriesExceededError` fırlatıldığını test et (sunucu hep 401 dönsün).
- [ ] `refresh()` fonksiyonu exception fırlattığında `RefreshFailedError` fırlatıldığını test et.

### Faz 3 — httpx Async Entegrasyonu + Eşzamanlılık Stres Testi

- [ ] `AsyncSingleFlightAuth`'u yaz (Bölüm 4.3).
- [ ] `pytest-asyncio` kur: `uv add --dev pytest-asyncio`.
- [ ] **Projenin en kritik testi** — `tests/test_concurrency_stress.py`:

```python
import asyncio
import httpx
import pytest
from singleflight_auth import AsyncSingleFlightAuth


@pytest.mark.asyncio
async def test_only_one_refresh_under_50_concurrent_401s(httpserver):
    state = {"token": "expired", "refresh_calls": 0}

    def handler(request):
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {state['token']}" and state["token"] == "fresh":
            return Response(status=200, response=b"ok")
        return Response(status=401)

    httpserver.expect_request("/protected").respond_with_handler(handler)

    async def refresh() -> str:
        await asyncio.sleep(0.05)  # gerçek bir ağ gecikmesini simüle et
        state["refresh_calls"] += 1
        state["token"] = "fresh"
        return "fresh"

    auth = AsyncSingleFlightAuth(get_token=lambda: state["token"], refresh=refresh)

    async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
        responses = await asyncio.gather(*[client.get("/protected") for _ in range(50)])

    assert all(r.status_code == 200 for r in responses)
    assert state["refresh_calls"] == 1  # <- BUNU KANITLAMAK PROJENİN TÜM AMACI
```

Bu test kırmızıdan yeşile geçtiğinde, kütüphanenin vaat ettiği şeyin çalıştığını **somut olarak** kanıtlamış olursun. Bunu README'nin en üstüne de koyacaksın (bkz. Bölüm 11).

- [ ] Aynı testi `ThreadPoolExecutor` ile sync `SingleFlightAuth` için de yaz.

### Faz 4 — requests Entegrasyonu

- [ ] `requests_auth.py`'ı yaz (Bölüm 4.4).
- [ ] `uv add --dev responses` (requests için mock/stub kütüphanesi) veya `pytest-httpserver`'ı burada da kullan (tutarlılık için ikinciyi öneririm).
- [ ] `tests/test_requests.py`: mutlu yol, max-retry, refresh-hata testleri (Faz 2 ile paralel).
- [ ] `ThreadPoolExecutor` ile eşzamanlılık stres testi (requests'in async karşılığı yok, sadece thread testi yeterli).

### Faz 5 — Edge Case'ler ve Sertleştirme

Bu fazda şu senaryoları düşün ve testler yaz:
- [ ] Refresh fonksiyonu **None** veya boş string döndürürse ne olur? (Muhtemelen `ValueError`.)
- [ ] Aynı anda hem 401 hem farklı bir hata kodu (500) dönen isteklerin karışması — 500'ler retry mantığına girmemeli.
- [ ] `is_unauthorized` callback'i özelleştirilebilir mi (bazı API'ler 401 yerine 403 veya özel bir hata kodu/gövdesi dönebilir) — evet, opsiyonel parametre olarak zaten tasarımda var, testini yaz.
- [ ] Çok kısa süreli ama çok sık refresh senaryosu (token her istekte expire oluyormuş gibi) — sonsuz döngüye girmiyor, `max_retries` doğru duruyor mu.
- [ ] `httpx.Client`'ın `base_url` ve relative path kombinasyonlarıyla uyumluluk.

### Faz 6 — Tip Güvenliği ve Lint

```bash
uv add --dev mypy ruff
uv run ruff check --fix .
uv run ruff format .
uv run mypy src
```

- [ ] `mypy.ini` veya `pyproject.toml` içinde `strict = true` ile sıfır hata almayı hedefle.
- [ ] `src/singleflight_auth/py.typed` boş dosyasını ekle — bu, paketinin tip bilgisi sunduğunu editörlere/mypy'ye bildirir (PEP 561).

### Faz 7 — Dokümantasyon

- [ ] `README.md` (detayları Bölüm 11'de).
- [ ] Her public sınıf/fonksiyon için docstring (Google veya NumPy stili, tutarlı ol).
- [ ] `CHANGELOG.md` — [Keep a Changelog](https://keepachangelog.com) formatında, `## [Unreleased]` başlığıyla başlat.
- [ ] `CONTRIBUTING.md` — nasıl katkı verilir, testler nasıl çalıştırılır (kısa, 1 sayfa yeter).

### Faz 8 — CI/CD

Detaylar Bölüm 9'da; bu fazda `.github/workflows/ci.yml`'ı yazıp her push/PR'da testlerin, lint'in ve tip kontrolünün otomatik çalıştığını doğrula.

### Faz 9-11 — Paketleme ve Yayın

Detaylar Bölüm 8 ve 9'da, ayrı ayrı ele alınıyor.

### Faz 12-13 — Duyuru ve Bakım

Detaylar Bölüm 12 ve 13'te.

---

## 7. Test Stratejisi — Neden Bu Kadar Önemli

Bu bir **eşzamanlılık** kütüphanesi; yanlış çalıştığında hata sessizdir (bazen çalışır, bazen çalışmaz — klasik race condition doğası). Bu yüzden:

1. **Determinizm için gerçek gecikme ekle.** Testlerde `refresh()` fonksiyonuna `await asyncio.sleep(0.05)` veya `time.sleep(0.05)` koy. Gecikme olmadan yazılan testler, race condition'ı yakalamakta başarısız olabilir çünkü her şey o kadar hızlı olur ki yarış hiç oluşmaz.
2. **`call_count` assert'i her testin can damarı.** "Kaç kez refresh çağrıldı" her zaman açıkça doğrulanmalı — sadece "istekler 200 döndü mü" yeterli değil, çünkü naif/hatalı bir implementasyon da (her istek kendi refresh'ini yapan) sonunda 200 dönebilir.
3. **En az 20-50 eşzamanlı istekle test et.** 2-3 istekle yazılan testler yarış durumunu tetiklemeyebilir; sayıyı yüksek tut.
4. **CI'da flaky testi yakalamak için `pytest-repeat` düşün** — aynı stres testini 10 kez art arda çalıştır (`uv add --dev pytest-repeat`, `@pytest.mark.repeat(10)`), tek seferlik "şans eseri geçti" testlerinden kaçın.

---

## 8. `pyproject.toml` — Tam İçerik

```toml
[project]
name = "singleflight-auth"
version = "0.1.0"
description = "httpx ve requests icin tek-ucus (single-flight) token yenileme: paralel 401'ler refresh'i sadece bir kez tetikler."
readme = "README.md"
requires-python = ">=3.9"
license = "MIT"
authors = [
    { name = "SENIN_ADIN", email = "sen@example.com" },
]
keywords = ["httpx", "requests", "auth", "jwt", "token-refresh", "concurrency", "single-flight", "401", "race-condition"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Typing :: Typed",
    "Topic :: Internet :: WWW/HTTP",
    "Topic :: Software Development :: Libraries :: Python Modules",
]

[project.optional-dependencies]
httpx = ["httpx>=0.24"]
requests = ["requests>=2.28"]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-httpserver>=1.0",
    "pytest-repeat>=0.9",
    "mypy>=1.10",
    "ruff>=0.6",
    "httpx>=0.24",
    "requests>=2.28",
]

[project.urls]
Homepage = "https://github.com/KULLANICI_ADIN/singleflight-auth"
Repository = "https://github.com/KULLANICI_ADIN/singleflight-auth"
Issues = "https://github.com/KULLANICI_ADIN/singleflight-auth/issues"
Changelog = "https://github.com/KULLANICI_ADIN/singleflight-auth/blob/main/CHANGELOG.md"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/singleflight_auth"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py39"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
strict = true
python_version = "3.9"
```

Bu yapı, güncel (2026) Python paketleme pratiğiyle uyumlu: <cite index="34-1">build backend olarak hatchling kullanmak, setup.py gibi eski araçlardan kaçınıp uv ile sorunsuz çalışan minimal ve sağlam bir yapılandırma sağlıyor</cite>.

---

## 9. PyPI'a Yükleme — Baştan Sona, Adım Adım

### 9.1 Hesap Hazırlığı

- [ ] [pypi.org](https://pypi.org) üzerinde hesap aç, **2FA'yı zorunlu olarak aktifleştir** (2026 itibarıyla PyPI hesaplarda güçlü kimlik doğrulamayı zorunlu tutuyor).
- [ ] [test.pypi.org](https://test.pypi.org) üzerinde de **ayrı** bir hesap aç (TestPyPI, gerçek PyPI'dan bağımsız bir kullanıcı veritabanına sahiptir).

### 9.2 Paket Adı Kontrolü

- [ ] `https://pypi.org/project/<isim>/` adresine git, 404 dönüyorsa isim müsait demektir.
- [ ] Aynı kontrolü `https://test.pypi.org/project/<isim>/` için de yap.
- [ ] İsmi bulur bulmaz **erken rezerve etmek** için boş bir `0.0.1` sürümünü hemen yükleyebilirsin (yaygın bir pratik), ama zorunlu değil.

### 9.3 Yerel Build ve TestPyPI Dry-Run

```bash
uv build
ls -R dist/
# beklenen çıktı: singleflight_auth-0.1.0-py3-none-any.whl ve .tar.gz
```

Yükleme öncesi paketin içeriğini gözle kontrol et:

```bash
unzip -l dist/singleflight_auth-0.1.0-py3-none-any.whl
```

- [ ] `src/singleflight_auth/*.py` dosyalarının hepsi orada mı?
- [ ] Testler (`tests/`) pakete **sızmamış** mı (yanlışlıkla dahil olmamalı)?

### 9.4 Trusted Publishing Kurulumu (Token Kullanmadan, OIDC ile)

2026 itibarıyla önerilen yöntem, uzun ömürlü API token'ları GitHub Secrets'a koymak **değil**, "Trusted Publishing" (OIDC) kullanmak: <cite index="28-1">GitHub Action'a küçük bir permissions bloğu ekleyerek, uv publish'in kısa ömürlü OIDC token'ları kullanarak doğrudan PyPI ile kimlik doğrulaması yapmasını sağlayabilirsin — hiç secret saklamana gerek kalmaz</cite>.

**Önce TestPyPI için:**

- [ ] `https://test.pypi.org/manage/account/publishing/` adresine git.
- [ ] "Create a new pending publisher" formunu doldur: <cite index="32-1">paket adını pyproject.toml'daki haliyle birebir gir, ardından GitHub kullanıcı adı, repo adı, workflow dosya adı</cite>.
- [ ] Kaydet.

**Sonra gerçek PyPI için (yayına hazır olduğunda):**

- [ ] `https://pypi.org/manage/account/publishing/` adresinde aynı işlemi tekrarla.

> **Dikkat:** <cite index="31-1">GitHub reposunu yeniden adlandırmak, başka bir hesaba taşımak veya workflow dosyasının adını değiştirmek trusted publishing'i bozar — OIDC token'ı artık PyPI'daki kayıtlı publisher ile eşleşmeyen bir owner/repo/workflow taşır ve yükleme başarısız olur</cite>. İsim/repo kararını erken ver, sık değiştirme.

### 9.5 GitHub Actions Workflow Dosyaları

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: uv sync --all-extras --dev
      - run: uv run ruff check .
      - run: uv run mypy src
      - run: uv run pytest -v --tb=short
```

`.github/workflows/publish-testpypi.yml` (manuel tetikleme, denemek için):

```yaml
name: Publish to TestPyPI

on:
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv build
      - run: uv publish --index testpypi --trusted-publishing always
```

`.github/workflows/publish.yml` (gerçek PyPI, sadece GitHub Release yayınlanınca tetiklenir):

```yaml
name: Publish to PyPI

on:
  release:
    types: [published]

permissions:
  id-token: write
  contents: read

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv build
      - run: uv publish --trusted-publishing always
```

`permissions: id-token: write` satırı **zorunlu** — <cite index="29-1">bu olmadan uv publish sessizce kimliksiz yüklemeye düşer, job "başarılı" görünür ama PyPI paketi reddeder</cite>.

### 9.6 TestPyPI'da Deneme

- [ ] `publish-testpypi.yml` workflow'unu GitHub Actions sekmesinden elle tetikle (`workflow_dispatch`).
- [ ] `https://test.pypi.org/project/singleflight-auth/` sayfasında paketin göründüğünü doğrula.
- [ ] Temiz bir sanal ortamda gerçekten kur ve dene:

```bash
uv venv /tmp/test-env
source /tmp/test-env/bin/activate
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ singleflight-auth
python -c "from singleflight_auth import SingleFlightAuth; print('OK')"
```

(`--extra-index-url` gerekli çünkü `httpx`/`requests` gibi bağımlılıkların TestPyPI'da olmayabilir, gerçek PyPI'dan çekilmeleri gerekir.)

### 9.7 Gerçek PyPI'a Yayın

- [ ] `pyproject.toml`'da versiyonu doğrula (`0.1.0`).
- [ ] `CHANGELOG.md`'ye `## [0.1.0] - TARİH` bölümü ekle.
- [ ] Git'te etiketle: `git tag v0.1.0 && git push origin v0.1.0`.
- [ ] GitHub'da "Releases" → "Draft a new release" → `v0.1.0` tag'ini seç → CHANGELOG'dan notları yapıştır → **Publish release**.
- [ ] Bu, `publish.yml`'daki `on: release: types: [published]` tetikleyicisini otomatik çalıştırır.
- [ ] Actions sekmesinde workflow'un yeşil bittiğini izle.
- [ ] `https://pypi.org/project/singleflight-auth/` sayfasında paketin canlı olduğunu doğrula — <cite index="31-1">sayfada, paketin hangi GitHub Actions çalıştırmasından üretildiğini doğrulayan bir provenance (kaynak doğrulama) bilgisi görünmeli</cite>.

---

## 10. Sürümleme Stratejisi

- **Semantic Versioning (SemVer)** kullan: `MAJOR.MINOR.PATCH`.
  - `0.x.y` iken her şey "kararsız/deneysel" kabul edilir, API kırılabilir.
  - `1.0.0`'a geçiş = "bu API'ye güvenebilirsiniz" sinyali; testler stabil, dokümantasyon tam olunca at.
- Her yeni sürümden önce `CHANGELOG.md`'yi güncelle — [Keep a Changelog](https://keepachangelog.com) formatı: `Added`, `Changed`, `Fixed`, `Removed` başlıkları.
- Versiyon numarasını **tek bir yerde** tut (`pyproject.toml`); ileride büyürse `hatch-vcs` ile git tag'lerinden otomatik türetmeyi düşünebilirsin (şimdilik gerek yok, manuel yeterli).

---

## 11. README Yapısı ve Dokümantasyon

README'nin sırası önemli — insanlar ilk 10 saniyede karar verir:

1. **Başlık + tek cümlelik değer önermesi.** "httpx ve requests için: paralel 401'ler artık refresh'i sadece bir kez tetikler."
2. **Rozetler (badges):** PyPI sürümü, Python sürüm desteği, lisans, CI durumu, test kapsamı (`shields.io` üzerinden).
3. **30 saniyelik örnek** — Bölüm 4.7'deki kod bloğu, tam olarak buraya.
4. **"Neden bu kütüphane" bölümü** — Bölüm 2'deki karşılaştırma tablosunu buraya koy, dürüstçe: `httpx-auth`/`requests_oauth2client`'a link ver, ne zaman onları kullanmaları gerektiğini söyle.
5. **Kurulum:** `pip install singleflight-auth[httpx]` / `[requests]`.
6. **API referansı** kısa, parametre parametre.
7. **"Nasıl çalışır" bölümü** — Bölüm 4.1'deki double-checked locking açıklamasını basitleştirip buraya koy; bir diyagram (ASCII veya basit bir görsel) eklemek çok değerli, insanlar mekanizmaya güvenir.
8. **Sınırlamalar** — Bölüm 3.2'deki "yapılmayanlar" listesi, açıkça.
9. **Katkı** → `CONTRIBUTING.md`'ye link.
10. **Lisans** → MIT.

---

## 12. Duyuru / Pazarlama Planı — Somut Adımlar

Sırayla, aynı gün hepsini birden yapma (izleyip öğren, sonra yay):

- [ ] **Gün 1:** `r/Python` alt redditinde "I built X" değil, "Show and Tell" flair'iyle paylaş; başlıkta problemi anlat, çözümü değil: *"Paralel API isteklerin 401 aldığında hepsi ayrı ayrı token yenilemeye çalışıyor mu? Bunu çözen küçük bir kütüphane yazdım."* Kod örneğini ve stres testini (Faz 3'teki test) doğrudan gönderiye göm — insanlar "kanıt" görmek ister.
- [ ] **Gün 3-4 (ilk tepkileri gördükten sonra):** Bir `dev.to` veya kişisel blog yazısı — "Neden yazdım, nasıl çözdüm" derinlemesine anlatım. httpx'in `auth_flow`/`async_auth_flow` ayrımındaki inceliği (Bölüm 4.3) burada paylaşmak, seni "gerçekten anlayan biri" olarak konumlandırır, sadece "paketleyen biri" değil.
- [ ] **Hafta 2:** Hacker News'te "Show HN: ..." başlığıyla paylaş (sadece bir kere, silinirse tekrar deneme, spam sayılır).
- [ ] Uygunsa `r/FastAPI`, `r/django`, `r/webdev` gibi ilgili topluluklarda, "araç tanıtımı" değil "problem çözümü" çerçevesiyle paylaş.
- [ ] `awesome-httpx` benzeri "awesome list" repolarını GitHub'da ara, varsa PR ile ekle.
- [ ] GitHub repo "Topics" alanına `httpx`, `requests`, `python`, `authentication`, `jwt`, `concurrency` etiketlerini ekle — GitHub aramasında bulunmanı sağlar.
- [ ] PyPI proje sayfasında `Project description` alanının README'den doğru render edildiğini kontrol et (görsel ilk izlenim önemli).

---

## 13. Gerçekçi Beklenti Yönetimi

- İlk ay birkaç yüz PyPI indirmesi **iyi bir sonuç** sayılır; bu alanın devleri (`httpx-auth`) haftalık yüz binlerce indirmeye sahip, yıllar süren güven inşasının ürünü. Kıyaslama yapma.
- Asıl kazanım: bu proje CV/portföyünde "async/await, threading, concurrency, test-driven development, CI/CD, açık kaynak paket yönetimi" konularında **somut kanıt**. Mülakatlarda "eşzamanlılıkla ilgili zorlayıcı bir proje anlat" sorusuna tam isabet bir cevabın olur.
- Gerçek kullanıcı geri bildirimi (issue, PR, "bunu prod'da kullanıyorum" yorumu) — indirme sayısından çok daha değerli bir sinyal; bunlara hızlı ve nazik cevap ver, ilk birkaç katkıda bulunan kişi projenin geleceğini şekillendirir.

---

## 14. İsim Önerileri ve Lisans

Aşağıdaki adaylar şu an (araştırma anı itibarıyla) PyPI'da **boş görünüyordu** — yine de yükleme öncesi mutlaka kendin `pypi.org/project/<isim>/` ile tekrar kontrol et, isimler hızlı tükenir:

- `singleflight-auth`
- `authcoalesce`
- `refresh-coalesce`
- `httpx-singleflight-auth`

Not: `singleflight` adı tek başına **dolu** — <cite index="43-1">Go'nun groupcache singleflight'ının Python portu olan, genel amaçlı bir çağrı-birleştirme kütüphanesi olarak zaten PyPI'da yayında</cite>, o yüzden tam bu ismi kullanma.

**Lisans:** MIT öner — hem `httpx-auth` hem `axios-auth-refresh` gibi bu alandaki emsal kütüphanelerin çoğu MIT kullanıyor, kullanıcı beklentisiyle uyumlu ve en az sürtünmeli seçim.

---

## 15. Yayına Kadar Özet Kontrol Listesi

- [ ] Faz 1-5: Çekirdek + httpx sync + httpx async + requests + edge case'ler tamam ve testli
- [ ] Faz 6: `mypy --strict` sıfır hata, `ruff check` temiz
- [ ] Faz 7: README, docstring'ler, CHANGELOG, CONTRIBUTING yazıldı
- [ ] Faz 8: CI yeşil (tüm Python sürümlerinde)
- [ ] PyPI + TestPyPI hesapları, 2FA aktif
- [ ] Paket adı kontrol edildi, rezerve edildi
- [ ] Trusted Publisher hem TestPyPI hem PyPI'da kuruldu
- [ ] TestPyPI'da denendi, temiz ortamda `pip install` ile doğrulandı
- [ ] `v0.1.0` etiketlendi, GitHub Release yayınlandı, gerçek PyPI'da canlı
- [ ] Duyuru planı (Bölüm 12) sırayla uygulanıyor

---

*Bu plan yaşayan bir belge — ilerledikçe geri dönüp güncelle, özellikle Faz 5'teki edge case listesini gerçek kullanım deneyiminle büyüt.*