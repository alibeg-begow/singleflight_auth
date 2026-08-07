# `singleflight-auth` — Derinlemesine İnceleme ve Hata Raporu

**İncelenen proje:** [github.com/alibeg-begow/singleflight_auth](https://github.com/alibeg-begow/singleflight_auth) · [pypi.org/project/singleflight-auth](https://pypi.org/project/singleflight-auth/)
**Sürüm:** 0.1.2 (PyPI'da 2 Ağustos 2026'da yayınlanmış; commit `9b91186`)
**İnceleme tarihi:** 7 Ağustos 2026
**Yöntem:** Repo klonlandı, `pip install -e ".[dev]"` ile kuruldu, `pytest` / `ruff` / `mypy --strict` çalıştırıldı, ardından her bulgu için **gerçek, çalıştırılabilir tekrar-üretim (repro) betikleri** yazılıp sonuçları doğrulandı. Aşağıdaki her hatanın gerçek çıktısı bu raporda gösterilmiştir — hiçbiri varsayıma dayanmıyor.

---

## Genel Durum

Projenin kod kalitesi kontrolleri **tamamen temiz**:

```
$ pytest -v            → 40 passed in 4.39s
$ ruff check .          → All checks passed!
$ mypy src --strict     → Success: no issues found in 5 source files
```

Kod stili düzenli, tip anotasyonları eksiksiz, docstring'ler iyi yazılmış. Ancak bu kontroller **statik**tir; kütüphanenin asıl vaadi olan "401 → refresh → retry" akışının **gerçek dünya senaryolarında** (aynı client'ın refresh içinde yeniden kullanılması, dosya/stream body'li istekler) davrandığı biçimi test etmiyorlar. Test suite'inde bu senaryolara dair **tek bir test bile yok** — aşağıdaki üç kritik hatanın CI'ı geçip PyPI'a kadar ulaşmasının nedeni de bu.

Proje çok yeni (ilk sürüm 2 Ağustos 2026, bu inceleme sırasında GitHub'da açık issue yok) — yani bu muhtemelen projenin aldığı ilk derinlemesine kod incelemesi.

---

## 🔴 Hata #1 (Kritik): `refresh()` aynı client/session'ı kullanırsa **kilitlenme (deadlock)**

### Sorun

`SyncCoordinator` ve `AsyncCoordinator`, sırasıyla düz `threading.Lock` ve `asyncio.Lock` kullanıyor (`src/singleflight_auth/_core.py`, satır 59 ve 111). **Bu kilit türleri reentrant (yeniden-giriş yapılabilir) değildir.**

`refresh()` fonksiyonu, `auth` nesnesinin takılı olduğu **aynı** `httpx.Client` / `httpx.AsyncClient` / `requests.Session` üzerinden bir istek atarsa — ki bu proxy, timeout, base_url, sertifika gibi ayarları tekrarlamamak için oldukça doğal bir tercihtir — ve o istek de 401 dönerse, şu zincir oluşuyor:

1. İlk istek 401 alır → `coordinator.resolve()` çağrılır → kilit alınır.
2. Kilit tutulurken `refresh()` çalıştırılır.
3. `refresh()`, aynı `auth`'a sahip client/session ile ikinci bir istek atar.
4. O istek de 401 alır → aynı `auth` nesnesi tekrar devreye girer → `coordinator.resolve()` **aynı thread/coroutine tarafından ikinci kez** çağrılır.
5. Kilit zaten aynı thread tarafından tutulduğu için `Lock.acquire()` **sonsuza dek bloklanır.**

Bunu hem `requests` hem de amiral gemisi örnek olan `httpx` senkron entegrasyonu için canlı olarak doğruladım:

```python
# requests entegrasyonu ile deadlock
session = requests.Session()

def refresh() -> str:
    r = session.post(refresh_url)   # AYNI session — auth zaten üzerinde
    return "new-token-" + str(r.status_code)

auth = RequestsSingleFlightAuth(get_token=lambda: "stale", refresh=refresh, max_retries=1)
session.auth = auth
session.get(protected_url, timeout=3)   # timeout HİÇBİR ŞEYİ KURTARMIYOR
```

**Gerçek çıktı:**
```
*** DEADLOCK CONFIRMED: worker thread is still alive after 8s ***
```

Aynısı `httpx.Client` için de geçerli:
```
*** DEADLOCK CONFIRMED (httpx sync) ***
```

Önemli detay: `session.get(..., timeout=3)` gibi bir HTTP zaman aşımı **hiçbir işe yaramıyor**, çünkü kilitlenme ağ katmanında değil, saf Python `Lock.acquire()` çağrısında oluşuyor. Bu, thread'in **kalıcı olarak** askıda kalması, sürecin sonunda kill edilmesi gerekmesi anlamına gelir — production'da bu, tek bir kullanıcının token'ı süresi dolmuşken refresh endpoint'i de hata verirse (ki bu tam da auth kesintisi anlarında olur) tüm worker havuzunu tüketebilecek bir senaryodur.

Async tarafta (`asyncio.Lock`) de aynı kök neden geçerlidir: kilit coroutine bazında reentrant değildir, dolayısıyla `AsyncSingleFlightAuth` ile de aynı yapıdaki bir kullanım o task'ı sonsuza dek askıda bırakır.

### Neden Önemli

- README'deki "30 saniyelik örnek" `refresh()` içinde modül seviyesinde `httpx.post(...)` kullanıyor (auth'suz, ayrı bağlantı) — bu yüzden tuzağa düşmüyor, ama **neden** düşmediği hiçbir yerde açıklanmıyor. Kullanıcı "zaten yapılandırılmış client'ımı kullanayım, tutarlı olsun" dediği an bu tuzağa düşer.
- Hata sessiz değil ama **görünür de değil**: exception fırlamıyor, log basmıyor, sadece donuyor.

### Önerilen Çözüm

1. README'ye büyük harflerle bir uyarı eklenmeli: *"refresh() fonksiyonu içinde, bu auth nesnesinin bağlı olduğu client/session'ı ASLA kullanmayın — ayrı, auth'suz bir client kullanın."* (Async örnekte zaten örtük olarak yapılıyor, ama açıkça söylenmiyor.)
2. Daha sağlam çözüm: `threading.RLock` yerine kilit + "şu an kilidi tutan thread/task" izleyen bir mekanizma kurup, aynı thread/task tekrar girdiğinde ya açıkça `RefreshFailedError`/yeni bir `ReentrantRefreshError` fırlatmak (sessiz donmak yerine gürültülü ve anlaşılır şekilde başarısız olmak).
3. En azından `resolve()` içine bir "refresh zaten devam ediyor, aynı thread tekrar girdi" tespiti eklenip anlamlı bir hata fırlatılmalı.

---

## 🔴 Hata #2 (Kritik): Stream/dosya gövdeli isteklerde retry sırasında **sessiz veri kaybı, donma veya çökme**

### Sorun

Hem `httpx_auth.py` (satır 106, 114 — `yield request`) hem de `requests_auth.py` (satır 119 — `response.request.copy()`), 401 sonrası retry'de **aynı request nesnesini** (veya onun sığ kopyasını) tekrar gönderiyor. Bu, body'si bellekte sabit `bytes`/`str`/`dict` (json=) olan sıradan API çağrıları için sorunsuz çalışır — çünkü httpx bunları tekrar tekrar okunabilen bir `ByteStream`'e çeviriyor.

Ama body bir **dosya, generator veya tek-seferlik akış** (stream) ise — ki dosya/görsel/belge yükleme uç noktaları bearer-token korumalı API'lerde son derece yaygındır — retry'de ciddi sorunlar çıkıyor. Beş farklı senaryoyu gerçek kütüphaneyle uçtan uca test ettim:

| Body türü | Kütüphane | Retry'de gerçekleşen | Kanıtlanmış mı? |
|---|---|---|---|
| Generator (`yield b"..."`) | httpx | `httpx.StreamConsumed` hatası fırlar | ✅ |
| Generator (`yield b"..."`) | requests | **Sessizce boş body gönderilir, sunucu 200 OK döner** | ✅ |
| Bilinen uzunluklu dosya (`open(...,"rb")`) | httpx | `h11._util.LocalProtocolError: Too little data for declared Content-Length` (anlaşılması zor, düşük seviyeli hata) | ✅ |
| Bilinen uzunluklu dosya (`open(...,"rb")`) | requests | **Sonsuza dek donar** (timeout verilmezse kalıcı hang) | ✅ |
| Uzunluğu bilinmeyen stream (pipe/proxy, `fileno()`/`seek()` yok) | httpx | **Sessizce boş body gönderilir, sunucu 200 OK döner** | ✅ |

En tehlikeli senaryo — **sessiz veri kaybı** — şu şekilde doğrulandı (gerçek `SingleFlightAuth` kullanılarak, kurgu değil):

```python
class PipedUploadStream:
    """subprocess çıktısı, proxy edilen bir upload, soket vb. gerçekçi bir senaryo"""
    def __init__(self, chunks): self._chunks, self._i = list(chunks), 0
    def read(self, n=-1):
        if self._i >= len(self._chunks): return b""
        c = self._chunks[self._i]; self._i += 1; return c
    def __iter__(self): return self
    def __next__(self):
        c = self.read()
        if not c: raise StopIteration
        return c

stream = PipedUploadStream([b"CRITICAL-", b"PAYLOAD-", b"MUST-NOT-BE-LOST"])
with httpx.Client(auth=auth, base_url=...) as client:
    resp = client.post("/upload", content=stream)
```

**Gerçek çıktı:**
```
Final response status: 200
Attempt 1 -> server received 33 bytes: b'CRITICAL-PAYLOAD-MUST-NOT-BE-LOST'
Attempt 2 -> server received 0 bytes: b''
```

Yani: ilk deneme 401 alıyor (token bayat), refresh tetikleniyor, **retry sunucuya 0 byte gönderiyor ve sunucu buna 200 OK diyor.** Çağıran kod `resp.status_code == 200` görüp yüklemenin başarılı olduğunu sanıyor — oysa veri tamamen kayboldu. Hiçbir exception, hiçbir uyarı yok. Aynı senaryo `requests` entegrasyonunda generator body ile birebir aynı sonucu veriyor.

Gerçek dosya (`open(..., "rb")`) durumunda `requests` tarafında ise sonuç **sonsuz donma**:
```
Raised: ReadTimeout - HTTPConnectionPool(...): Read timed out. (read timeout=5)
Attempt 1 -> server received 32 bytes: b'REQUESTS-FILE-UPLOAD-PAYLOAD-XYZ'
```
(Timeout parametresi verilmezse bu istek **hiçbir zaman** dönmez — `requests`'in varsayılan davranışı zaten timeout'suz olduğundan.)

### Neden Önemli

Kütüphanenin tüm satış noktası "401'leri güvenle koordine et"; ama en yaygın gerçek dünya kullanım senaryolarından biri olan dosya/stream yükleme için **retry mekanizması ya veriyi sessizce siliyor ya da uygulamayı donduruyor.** Bu, README'deki "Limitations" tablosunda da hiç bahsedilmiyor.

### Önerilen Çözüm

1. **En güvenli düzeltme:** Retry mantığından önce request body'sini `request.read()` (httpx) ile zorla belleğe okuyup sabit `bytes`'a çevirmek — böylece her retry aynı, tekrar okunabilir veriyi gönderir. `requests` tarafında da `PreparedRequest.copy()` öncesi body tek seferlik bir akışsa tamamını `bytes`'a okuyup `new_request.body`'yi buna eşitlemek.
2. Bu her zaman mümkün değilse (çok büyük dosyalar bellek sorunları yaratabilir), en azından **retry edilemeyen bir body tespit edildiğinde açık ve anlaşılır bir exception fırlatılmalı** (örn. `NonRewindableBodyError`) — sessiz veri kaybı veya kriptik düşük seviyeli hatalar yerine.
3. README'nin "Limitations" tablosuna bu kısıtlama eklenmeli: *"Stream/dosya/generator body'li istekler retry-güvenli değildir."*
4. Test suite'e dosya/stream body senaryoları için regresyon testleri eklenmeli — şu an bu tamamen test edilmiyor.

---

## 🟠 Hata #3 (Orta): `requests` entegrasyonunda `MaxRetriesExceededError` fırlatılırken **bağlantı sızıntısı**

### Sorun

`src/singleflight_auth/requests_auth.py`, satır 107-111:

```python
retried: int = getattr(response.request, "_sf_retry_count", 0)
if retried >= self._max_retries:
    raise MaxRetriesExceededError(...)   # ← response.content HİÇ okunmadan fırlatılıyor

stale_token: str = getattr(response.request, "_sf_token_used", "")
new_token = self._coordinator.resolve(stale_token=stale_token)

response.content  # noqa: B018 — bağlantıyı pool'a geri vermek için
```

`response.content`'e erişim, bağlantıyı `urllib3` connection pool'una geri bırakmanın standart `requests` yoludur (kütüphanenin kendisi de bunu başarı yolunda doğru yapıyor). Ama `MaxRetriesExceededError` fırlatılan satırda bu erişim **hiç gerçekleşmiyor.** `requests.Response` nesnesinin sızıntıyı önleyen bir `__del__` metodu da yok (`requests/models.py`'de sadece açık `close()` metodu var, otomatik temizlik yok).

Bunu, `_on_response`'u içerden gözlemleyen bir "spy" ile doğruladım:

```
MaxRetriesExceededError raised, as expected.
Retry count on the response that triggered the raise: 1
Was THAT response body consumed (._content_consumed)?: False
Was THAT response's raw connection closed/released?: False
```

Yani hatayı tetikleyen son 401 yanıtının bağlantısı **açık ve pool'a iade edilmemiş** durumda exception fırlatılıyor. Bağlantı sonunda Python'un çöp toplayıcısı (garbage collector) tarafından temizlenecek, ama bu zamana bağlı ve garanti değil — özellikle exception traceback'leri nesneleri referans zincirinde daha uzun süre canlı tutabildiğinden. Yaygın bir auth kesintisi sırasında (refresh sürekli başarısız oluyor, her istek `MaxRetriesExceededError` alıyor) bu durum `HTTPAdapter`'ın varsayılan (küçük, host başına 10) bağlantı havuzunu zamanla tüketebilir.

**Önemli:** `httpx` entegrasyonu bu sorundan etkilenmiyor — httpx'in kendi `_send_handling_auth` mekanizması, auth akışı bir exception fırlattığında `response.close()`'u kendi içinde garanti ediyor (`httpx/_client.py`, `except BaseException as exc: response.close(); raise exc`). Sorun sadece `requests_auth.py`'nin kendi hata yolunda.

### Önerilen Çözüm

```python
if retried >= self._max_retries:
    response.content  # bağlantıyı pool'a bırak, sonra fırlat
    raise MaxRetriesExceededError(...)
```
ya da daha sağlam biçimde `try/finally` içine alınmalı.

---

## 🟡 Diğer Bulgular (Düşük/Orta Öncelik)

### 4. "Single-flight" garantisi tek bir paylaşılan `auth` örneğine bağlı — bu hiçbir yerde belgelenmemiş

Kilit (`threading.Lock`/`asyncio.Lock`), `SyncCoordinator`/`AsyncCoordinator` **örneğinin** bir özelliği. Yani koordinasyonun çalışması için **aynı `auth` nesnesinin** tüm client/session'lar arasında paylaşılması **şart**. Kullanıcı her istekte veya her `Client()` çağrısında yeni bir `SingleFlightAuth(...)` oluşturursa (örneğin FastAPI'de her request'te yeniden kurulan bir dependency içinde), her nesnenin kendi kilidi olacağından **kütüphanenin tüm amacı olan "N eşzamanlı 401 → tam olarak 1 refresh" garantisi tamamen ortadan kalkar** — sessizce, hiçbir uyarı vermeden. README'de "auth nesnesini uygulama başlangıcında bir kez oluşturup paylaşın" gibi açık bir uyarı yok.

**Öneri:** README'ye ve docstring'lere kalın harflerle bu kısıtlama eklenmeli.

### 5. Aynı sınıf adı iki modülde tekrar kullanılıyor

`httpx_auth.SingleFlightAuth` ve `requests_auth.SingleFlightAuth` — birebir aynı isim, farklı sınıflar. `__init__.py` bunu `RequestsSingleFlightAuth` diye takma adla dışa aktararak akıllıca çözmüş, ama bir kullanıcı `from singleflight_auth.requests_auth import SingleFlightAuth` şeklinde alt modülden doğrudan import ederse (ki bazı IDE'ler bunu otomatik önerir) ve aynı dosyada httpx sürümünü de import etmek isterse isim çakışması yaşar. Küçük ama kolayca önlenebilir bir netlik sorunu.

### 6. Sürüm tarihi tutarsızlığı

`CHANGELOG.md`, 0.1.2 sürümünü **3 Ağustos 2026** olarak gösteriyor; PyPI sayfası ise **2 Ağustos 2026** olarak listeliyor. Küçük bir belge tutarsızlığı, muhtemelen zaman dilimi farkından kaynaklanıyor, işlevsel bir etkisi yok.

### 7. Test kapsamı boşluğu (kök neden)

Yukarıdaki üç kritik/orta hatanın hiçbiri mevcut 40 testten hiçbiri tarafından yakalanmıyor. CI sadece "mutlu yol" senaryolarını, `is_unauthorized` özelleştirmesini ve max_retries sınırlarını test ediyor — `refresh()`'in aynı client'ı kullanması, stream/dosya body, veya hata yollarında bağlantı yönetimi hiç test edilmiyor. Bu üç alan için regresyon testleri eklenmesi, projenin "production-ready" iddiasını gerçek anlamda desteklemesi için gerekli.

---

## Öncelik Sırasına Göre Özet

| # | Hata | Önem | Etkilenen Modül(ler) |
|---|---|---|---|
| 1 | `refresh()` aynı client'ı kullanınca deadlock | 🔴 Kritik | Hepsi (httpx sync/async, requests) |
| 2 | Stream/dosya body'de retry → veri kaybı/donma/çökme | 🔴 Kritik | httpx, requests |
| 3 | `MaxRetriesExceededError`'da bağlantı sızıntısı | 🟠 Orta | Sadece requests |
| 4 | Tek paylaşılan instance zorunluluğu belgelenmemiş | 🟡 Orta (dokümantasyon) | Hepsi |
| 5 | Aynı sınıf adının iki modülde tekrarı | 🟢 Düşük | Kod netliği |
| 6 | PyPI/CHANGELOG tarih uyuşmazlığı | ⚪ Önemsiz | Dokümantasyon |
| 7 | Kritik senaryolar için test eksikliği | 🟡 Süreç önerisi | Test suite |

## Sonuç

Kod stili, tip güvenliği ve "mutlu yol" mantığı gayet temiz ve iyi düşünülmüş. Ama kütüphanenin çekirdek vaadi olan *"refresh'i güvenle koordine et"* iki gerçekçi ve yaygın senaryoda (refresh'in aynı client'ı kullanması, stream/dosya body'li istekler) ciddi biçimde bozuluyor — biri tam bir deadlock'a, diğeri sessiz veri kaybına yol açıyor. Bunlar teorik uç durumlar değil; ikisi de production'da doğal bir şekilde ortaya çıkabilecek, üstelik en yaygın kullanım kalıplarına (paylaşılan client, dosya yükleme) çok yakın senaryolar. 0.1.x aşamasında olduğu için bunları şimdi düzeltmek, kütüphane daha geniş kullanım kazanmadan önce mümkün.
