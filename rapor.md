# singleflight-auth - Kapsamlı Proje Analiz Raporu

Bu rapor, `singleflight-auth` projesinin mimari kararlarını, dosya yapısını, içerdikleri kod bloklarını ve seçilen algoritmaların teknik nedenlerini en ince ayrıntısına kadar incelemek üzere hazırlanmıştır.

---

## 1. Proje Özeti ve Çözülen Temel Sorun (Race Condition)

Büyük çaplı uygulamalarda (örneğin binlerce eşzamanlı istek atan bir mikroservis), yetki belgesi (token) süresi dolduğunda, tüm istekler aynı anda `401 Unauthorized` hatası alır. Standart kütüphanelerde (httpx, requests) bu durumu yönetmek için yazılan kodlar, genellikle 401 alan *her bir isteğin* token yenileme (`refresh()`) fonksiyonunu çağırmasına neden olur. 

Örneğin, 50 istek aynı anda 401 alırsa, 50 kere token yenileme isteği atılır. Bu durum:
1. Yetkilendirme sunucusuna (Auth Server) gereksiz yük bindirir (Auth Cache Stampede).
2. Sonradan gelen token'lar öncekileri ezer ve tutarsızlıklara yol açar.

**singleflight-auth** kütüphanesi, bu "Race Condition" (Yarış Durumu) problemini "Double-Checked Locking" (Çift Kontrollü Kilitleme) algoritması ile çözer. 50 istek 401 alsa bile, `refresh()` fonksiyonu **sadece 1 kez** çağrılır. Geriye kalan 49 istek, sıraya girerek yeni alınan token'ı kullanır.

---

## 2. Mimari Yaklaşım ve Yöntem Seçimleri

Kütüphane geliştirilirken alınan en kritik karar: **Senkron ve Asenkron yapıların tamamen ayrıştırılmasıdır.**

Python'da `httpx` kütüphanesi hem senkron (`httpx.Client`) hem asenkron (`httpx.AsyncClient`) yapıyı destekler. Eğer kimlik doğrulama akışında standart bir `threading.Lock()` kullanılsaydı, asenkron `httpx.AsyncClient` kullanan bir geliştiricinin tüm *event loop'u* (olay döngüsü) kilitlenir ve asenkron programlamanın tüm avantajı yok olurdu. 

Bu nedenle kütüphane;
- Senkron (Thread tabanlı) işlemler için `threading.Lock`
- Asenkron (Coroutine tabanlı) işlemler için `asyncio.Lock`
kullanacak şekilde iki farklı "Coordinator" (Koordinatör) ve iki farklı "Auth Flow" sınıfı üzerine inşa edilmiştir.

---

## 3. Detaylı Kaynak Kod Analizi (`src/singleflight_auth/`)

### 3.1. `_core.py` (Kilitlenme ve Senkronizasyon Merkezi)
Bu dosya projenin kalbidir. HTTP kütüphanelerinden (requests, httpx) tamamen bağımsız bir şekilde kilit (lock) mantığını uygular.

#### `SyncCoordinator` Sınıfı (Senkron Yapılar İçin)
```python
class SyncCoordinator(Generic[T]):
    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], T],
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._lock = threading.Lock() # Thread tabanlı kilitleme

    def resolve(self, stale_token: T) -> T:
        with self._lock: # Kilit alınana kadar thread bekler
            current = self._get_token()
            # Double-Check: Kilit alınırken başka biri token'ı yeniledi mi?
            if current != stale_token:
                return current # Başkası yenilemiş, hazır olanı dön.
            # Kimse yenilememiş, yarışı biz kazandık. Yenile ve dön.
            return self._refresh()
```
**Neden Double-Checked Locking?**
Çünkü bir thread 401 alıp kilidi beklerken (örneğin 2. sırada), 1. sıradaki thread çoktan token'ı yenileyip kilidi bırakmış olabilir. Kilit alındıktan sonra token'ın değişip değişmediğini kontrol etmek (`current != stale_token`), gereksiz yere ikinci kez `refresh()` çağrılmasını engeller.

#### `AsyncCoordinator` Sınıfı (Asenkron Yapılar İçin)
```python
class AsyncCoordinator(Generic[T]):
    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], Awaitable[T]],
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._lock = asyncio.Lock() # Event loop'u durdurmayan asenkron kilit

    async def resolve(self, stale_token: T) -> T:
        async with self._lock: # Kilit beklenirken coroutine 'yield' eder, sistem donmaz.
            current = self._get_token()
            if current != stale_token:
                return current
            return await self._refresh()
```
**Farkı Nedir?**
Tamamen `asyncio.Lock` kullanımına dayalıdır. `async with self._lock:` sayesinde, bir coroutine kilidi beklerken Python'un olay döngüsü diğer görevleri işletmeye devam eder. Performans kaybı yaşanmaz.

---

### 3.2. `httpx_auth.py` (`httpx` Entegrasyonu)
Bu dosya, `_core.py`'daki koordinatörleri `httpx` kütüphanesine bağlar. `httpx`'in `Auth` sınıfından türetilir.

#### `SingleFlightAuth` Sınıfı
`httpx.Client` için tasarlanmıştır. 
```python
class SingleFlightAuth(httpx.Auth):
    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request # İsteği gönder ve yanıtı al

        retries = 0
        # 401 alınırsa ve deneme hakkı varsa döngüye gir
        while self._is_unauthorized(response) and retries < self._max_retries:
            retries += 1
            new_token = self._coordinator.resolve(stale_token=token) # Kilit mantığı devreye girer
            token = new_token
            request.headers["Authorization"] = f"Bearer {new_token}"
            response = yield request # Yenilenen token ile isteği tekrar gönder
        
        # Deneme hakkı bittiği halde 401 alınıyorsa hata fırlat
        if self._is_unauthorized(response) and retries >= self._max_retries:
            raise MaxRetriesExceededError(...)
```
**Kodun Amacı:** `httpx`, auth flow (yetki akışı) mekanizmasında Python'un `generator` (yield) yapısını kullanır. İlk `yield request` ile istek gider. Eğer yanıt 401 (`_is_unauthorized`) ise döngü tetiklenir, `SyncCoordinator` üzerinden token tek bir sefer yenilenir (veya yenilenmiş token alınır) ve `yield` ile istek tekrarlanır.

Aynı yapının asenkron versiyonu olan `AsyncSingleFlightAuth` sınıfında ise `async_auth_flow` metodu kullanılır ve `new_token = await self._coordinator.resolve(...)` şeklinde asenkron kilit beklenir.

---

### 3.3. `requests_auth.py` (`requests` Entegrasyonu)
`requests` kütüphanesinin yapısı `httpx` gibi gelişmiş bir "generator auth flow" mekanizmasına sahip değildir. "Response Hook" (yanıt kancası) kullanılarak kilitlenme mekanizması çözülmüştür.

```python
class SingleFlightAuth(requests.auth.AuthBase):
    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        # DURUM YÖNETİMİ: Thread'ler karışmasın diye değerleri request nesnesine gizliyoruz!
        request._sf_token_used = token
        request._sf_retry_count = 0
        request.register_hook("response", self._on_response) # Kancayı tak
        return request

    def _on_response(self, response: requests.Response, **kwargs: object) -> requests.Response:
        if not self._is_unauthorized(response): return response # Sorun yoksa dön

        # Sınır kontrolü (Gizli değişkenlerden oku)
        retried = getattr(response.request, "_sf_retry_count", 0)
        if retried >= self._max_retries: raise MaxRetriesExceededError(...)

        stale_token = getattr(response.request, "_sf_token_used", "")
        new_token = self._coordinator.resolve(stale_token=stale_token)

        # KRTİTİK NOKTA: response.content çağrılarak bağlantı connection pool'a iade edilir.
        response.content  

        # Yeni bir kopya istek yaratıp yolla
        new_request = response.request.copy()
        new_request.headers["Authorization"] = f"Bearer {new_token}"
        new_request._sf_token_used = new_token
        new_request._sf_retry_count = retried + 1

        new_response = response.connection.send(new_request, **kwargs)
        new_response.history = [*response.history, response]
        return self._on_response(new_response, **kwargs) # Recursion (tekrar kontrole gir)
```
**Neden Bu Yöntem Seçildi?**
`requests`, global state tutmaya çok müsaittir. Eğer `_sf_token_used` gibi değişkenleri sınıf (class) seviyesinde tutsaydık (örneğin `self.retried = 0`), çoklu iş parçacıklarında (multi-threading) 50 farklı thread aynı değişkene yazmaya çalışacak ve sistem çökecekti. Bunun yerine her bir `PreparedRequest` nesnesinin *üzerine* dinamik değişken (`_sf_token_used`) eklenerek tam bir İzolasyon (Thread-Safety) sağlanmıştır. Ayrıca `response.content` okunarak HTTP bağlantısının havuza (connection pool) iade edilmesi zorlanmış, bellek ve soket sızıntısının (socket leak) önüne geçilmiştir.

---

### 3.4. `_exceptions.py`
Sadece iki adet özel hata barındırır.
```python
class RefreshFailedError(Exception):
    # Kullanıcının verdiği refresh() fonksiyonu içinde hata çıkarsa fırlatılır.
    # Exception chaining (__cause__) ile orijinal hatayı yutar gibi yapmadan gösterir.

class MaxRetriesExceededError(Exception):
    # Refresh() başarılı oldu ama sunucu inatla 401 dönmeye devam ediyor.
    # Sonsuz while döngüsüne girmemek için max_retries aşıldığında fırlatılır.
```

---

## 4. Test Mimarisi ve İspat (`tests/`)

Projenin testleri sıradan birim testleri değil, eşzamanlılığın gerçekten çalışıp çalışmadığını "ispatlayan" testlerdir.

### 4.1. `test_concurrency_stress.py` (Kritik İspat Testi)
Bu dosyadaki test, projenin başarılı olup olmadığını kanıtlar. 50 asenkron istek aynı anda 401 almak üzere yerel sunucuya yollanır.

```python
@pytest.mark.asyncio
async def test_only_one_refresh_under_50_concurrent_401s(httpserver: HTTPServer) -> None:
    state = {"token": "expired", "refresh_calls": 0}

    async def refresh() -> str:
        await asyncio.sleep(0.05)  # Gerçek ağ gecikmesi (network latency) simülasyonu
        state["refresh_calls"] += 1
        state["token"] = "fresh"
        return "fresh"

    auth = AsyncSingleFlightAuth(get_token=lambda: state["token"], refresh=refresh)

    # Aynı anda 50 istek başlatılır
    async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
        responses = await asyncio.gather(*[client.get("/protected") for _ in range(50)])

    assert all(r.status_code == 200 for r in responses) # Tüm 50 istek sonunda başarıyla tamamlanmalı
    # EN KRİTİK ASSERT: Refresh fonksiyonu sadece 1 KERE çalışmış olmalı.
    assert state["refresh_calls"] == 1 
```
Eğer Single-Flight algoritmasında ufak bir mantık hatası olsaydı (örneğin kilit zamanlaması), `refresh_calls` 50 olurdu. `asyncio.sleep(0.05)` kullanılarak tüm görevlerin 401 yanıtını alıp kilidi beklemeye geçmesi için yeterli zaman verilmiş ve kasti bir Race Condition oluşturulmuştur. Testin `refresh_calls == 1` diyerek geçmesi algoritmanın kusursuzluğunu kanıtlar.

Aynı testin thread (iş parçacığı) bazlı versiyonu `test_only_one_refresh_under_50_concurrent_401s_sync` fonksiyonunda `ThreadPoolExecutor` kullanılarak yapılmıştır.

### 4.2. `test_edge_cases.py` (Sınır Durumları)
Geliştiricilerin yanlış kullanımlarına karşı kütüphanenin çökmemesi test edilir:
- **`refresh()` fonksiyonu boş (`""`) dönerse:** Kod bunu tespit eder ve token başarıyla yenilenmediği için `RefreshFailedError` fırlatır.
- **Karışık Durumlar (Mixed Status):** Sunucu bazen 401 bazen 500 (Internal Server Error) dönüyorsa? Test bunu doğrular: Kütüphane asla 500 hataları için kilit alıp token yenilemeye çalışmaz. Sadece `is_unauthorized` filtresine uyan durumlar tetikleyici olur.

---

## 5. Proje Yapılandırma ve Kalite Standartları

### 5.1. `pyproject.toml`
Modern Python standartlarında yapılandırılmıştır (`setup.py` kullanılmamıştır).
- **`hatchling`**: Paketleme ve build işlemlerini yönetir.
- **Tip İpuçları (Type Hints)**: `[tool.mypy]` ayarlarında `strict = true` yapılmıştır. Projede %100 tip güvenliği şart koşulmuştur. Herhangi bir `Any` kullanımı veya tip belirtilmeyen dönüş yasaklanmıştır. Ek olarak kodun içinde `py.typed` dosyası konularak IDE'lerin (VSCode, PyCharm) kütüphaneyi tipli olarak algılaması sağlanmıştır.
- **Linter (Ruff)**: `[tool.ruff]` ile piyasadaki en hızlı formatter/linter olan Ruff kullanılmıştır. Kuralları (`E`, `F`, `I`, `UP`, `B`, `ASYNC`) çok katıdır, gereksiz importlar, asenkron antipattern'leri (ASYNC) anında yakalar.
- **Opsiyonel Bağımlılıklar (Optional Dependencies)**: Kütüphane varsayılan olarak ne `httpx`'e ne de `requests`'e doğrudan bağlıdır. Kullanıcı sadece kullandığını kurar. (Örn: `pip install singleflight-auth[httpx]`). Bu sayede ortamlar gereksiz yere şişmez.

### 5.2. `README.md`
README dosyası projenin niş yapısına uygun olarak tasarlanmıştır. "Neden bu kütüphane?" (Why This Library?) tablosu eklenerek pazar konumlandırması yapılmış, `httpx-auth` (Tüm OAuth2 akışlarını barındıran kütüphane) ile farklılıkları açıkça anlatılmıştır. "Kendi yenileme mantığını getir, eşzamanlılığı biz halledelim" felsefesi (Bring your own refresh logic) mükemmel bir şekilde dokümante edilmiştir.

---

## Genel Sonuç

`singleflight-auth`, büyük çaplı sistemlerin baş belası olan kilitlenme ve önbellek yığılmaları sorununu; standart kütüphanelerin (httpx, requests) yapısını bozmadan, son derece zarif ve hafif (lightweight) bir mimari ile çözmüştür. 

Eşzamanlı kod yazmanın (async/await, multithreading) getirdiği tehlikeleri (thread çakışmaları, event loop bloklanmaları) mimari aşamada izole etmesi, tip güvenliğini %100 sağlaması ve 50 istekli paralel kanıt testlerini geçmesi bu paketin **production-ready** (canlı sisteme hazır) ve yüksek standartlara sahip bir yazılım olduğunu göstermektedir.
