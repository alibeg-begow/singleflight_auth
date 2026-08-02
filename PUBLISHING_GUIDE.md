# 🚀 PyPI Yayınlama Rehberi (PUBLISHING_GUIDE.md)

Bu rehber, projenin tamamen hazır olduktan sonra gerçek dünyaya (TestPyPI ve PyPI) nasıl yayınlanacağını anlatır. Github Actions ile "Trusted Publishing" (OIDC) kullanarak güvenli, şifresiz ve modern bir yayın süreci kurduk.

## 1. Hazırlık ve Hesap Kurulumu

1. **Hesapları Açın:**
   - Gerçek PyPI: [pypi.org/account/register/](https://pypi.org/account/register/)
   - Test PyPI: [test.pypi.org/account/register/](https://test.pypi.org/account/register/)
   *(İkisi birbirinden bağımsız sistemlerdir, ikisine de kayıt olmanız gerekir.)*
2. **2FA (İki Faktörlü Doğrulama) Aktif Edin:**
   PyPI artık tüm yayıncılar için 2FA'yı zorunlu kılıyor. Hesap ayarlarınızdan aktif etmeyi unutmayın.

## 2. Trusted Publishing (OIDC) Ayarları

Artık token veya şifre kopyalamanıza gerek yok! GitHub Actions'ın doğrudan PyPI ile yetkilendirilmesini (Trusted Publishing) sağlayacağız.

### TestPyPI İçin:
1. https://test.pypi.org/manage/account/publishing/ adresine gidin.
2. Formu doldurun:
   - **PyPI Project Name:** `singleflight-auth`
   - **Owner:** `alibeg-begow` *(GitHub kullanıcı adınız)*
   - **Repository name:** `singleflight_auth` *(GitHub repo adınız)*
   - **Workflow name:** `publish-testpypi.yml`
3. "Add" butonuna basın.

### Gerçek PyPI İçin:
1. https://pypi.org/manage/account/publishing/ adresine gidin.
2. Formu doldurun:
   - **PyPI Project Name:** `singleflight-auth`
   - **Owner:** `alibeg-begow`
   - **Repository name:** `singleflight_auth`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
3. "Add" butonuna basın.

> ⚠️ **Uyarı:** GitHub kullanıcı adı, repo adı veya yml dosya adlarından birisi ileride değişirse, bu ayarı PyPI'dan tekrar yapmanız gerekir. Aksi takdirde CI yetkilendirme hatası verir.

## 3. TestPyPI Üzerinde Deneme Yayınlama

Herhangi bir hata varsa gerçek PyPI'da kalıcı olmaması için önce TestPyPI'da deneme yapacağız.

1. GitHub Reponuza gidin → **Actions** sekmesine tıklayın.
2. Sol taraftan **Publish to TestPyPI** workflow'unu seçin.
3. Sağ taraftaki **Run workflow** butonuna basarak manuel olarak tetikleyin.
4. Başarıyla tamamlandığında [test.pypi.org/project/singleflight-auth/](https://test.pypi.org/project/singleflight-auth/) adresinde paketinizi görmelisiniz!
5. Yerel bilgisayarınızda kurup deneyin:
   ```bash
   pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ singleflight-auth
   ```

## 4. Gerçek PyPI'a Yayın (Production)

TestPyPI'da her şey sorunsuzsa, sıra gerçek yayınlamada:

1. `pyproject.toml` dosyasındaki versiyonun (`0.1.0`) doğru olduğundan emin olun.
2. `CHANGELOG.md` dosyasına bu sürümde yaptıklarınızı ekleyin.
3. Değişiklikleri pushlayın.
4. GitHub sayfanızda sağ taraftaki **Releases** kısmına gidin ve **Draft a new release** diyin.
5. Yeni tag oluşturun: `v0.1.0`. Başlık olarak versiyon numarasını verebilirsiniz.
6. **Publish release** butonuna basın.

Bunu yaptığınız anda, arkada yazdığımız `publish.yml` tetiklenecek ve projenizi otomatik build edip PyPI'a gönderecek.
İşlem bitince paketinizi [pypi.org/project/singleflight-auth/](https://pypi.org/project/singleflight-auth/) adresinde görebileceksiniz! 🎉

---

## 5. Lansman (Duyuru Önerileri)

Artık paketiniz var, peki insanlar nasıl haberdar olacak?

1. **Reddit (r/Python):** "I built X" değil, "Show and Tell" olarak paylaşın.
   *Örnek Başlık:* "Paralel API istekleriniz 401 aldığında hepsi ayrı ayrı token mi yenilemeye çalışıyor? Bunu tekilleştiren hafif bir kütüphane yazdım."
   *Stres testindeki kod bloğunu mutlaka paylaşın, kanıt görmek isterler.*
2. **Dev.to / Medium / Blog:** "Race condition sorununu nasıl çözdüm?" tarzında derin teknik bir yazı yazın. Sadece paketi tanıtmayın, `httpx` auth flow mimarisinden ve double-checked locking mantığından bahsedin.
3. **Topluluklar:** Hacker News, ilgili Telegram/Discord grupları (sadece izin veriliyorsa).
4. **Github Listeleri:** Bulabildiğiniz `awesome-httpx` vb. repository'lere PR açıp projenizi ekleyin.
