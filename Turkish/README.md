# CTLux: proje dosyalarından Radiance sahnesine

[Türkçe kaynak kod ve testler](source/README.md)

Ana İngilizce sürüm depo kökündeki `core/` ve `tests/` klasörlerindedir.
Bu sayfadaki Türkçe çıktı ve API adları için komutları `Turkish/source` klasöründen çalıştırın.
Türkçe testler bu klasördeki Türkçe `core` paketiyle çalışır

English: [README.md](../README.md).

CTLux, desteklenen Lumion® 10 ve DIALux evo proje kayıtlarını Radiance
sahnelerine dönüştürür. Bu depo, CTLux’un komut satırından çalışan çekirdeğini içerir.
Çeviri, dosyalardan statik geometri ve ışık kayıtlarını çıkarır, CTScene JSON ve
Radiance sahnesi yazar. `products`, DIALux evo veya üretici fotometri
dosyasındaki aydınlatma ürünlerini IES dosyalarına ve bir ürün tablosuna çıkarır.
`glare`, çevrilmiş sahnede yaşlı gözlemci için kamaşma analizi yapar: bir
bakış noktasından görülen kamaşma kaynaklarını bulur, her kaynağın arkasındaki
cismi adıyla eşler ve yaşa göre örtü parlaklığını sahneden hesaplar.
Yerel dosyalar ve yerel programlarla çalışır.

Bu bağımsız projenin Act-3D B.V. veya DIAL GmbH ile bağlantısı yoktur, bu şirketler
tarafından onaylanmaz veya desteklenmez. Lumion®, Act-3D B.V. şirketinin, DIALux,
DIAL GmbH şirketinin markasıdır. Ürün adları kaynak uygulamaları ve
bunların girdi dosyası biçimlerini belirtir.

## Gereksinimler ve kullanım

Linux x86-64 üzerinde Python 3.14.7 ile test edildi. Daha eski Python
sürümleriyle uyumluluk doğrulanmadı. Çekirdeğin zorunlu üçüncü taraf Python
paketi yoktur.

- Radiance: IES/LDT ve EVO'dan kurtarılan fotometri için `ies2rad`, `xform`.
  `products` komutunda EVO girdisi için `ies2rad`.
  `glare` için `oconv`, `rpict`, `evalglare`, `rtrace`, `pextrem`, `pcond`, `ra_ppm`.
  Aşağıdaki çizim için `oconv`, `rpict`, `ra_ppm`.
  Çizim API'si ayrıca `obj2mesh`, `obj2rad`, `rpiece`,
  `vwrays`, `pfilt`, `pcond`, `pextrem`, `falsecolor`, `getbbox`, `ra_tiff` kullanabilir
- İsteğe bağlı Assimp programı: STL, 3DS, GLB, glTF, DAE ve FBX import işlemleri
- İsteğe bağlı `usd-core`: USD girdileri için, komutu çalıştıran Python'a kurulu olmalı
- İsteğe bağlı `jsonschema`: bağımsız şema testi için. Çekirdeğin kendi CTScene
  doğrulayıcısı her zaman çalışır

Programları `PATH` üzerinden erişilebilir yapın. Alternatif olarak
`RADIANCE_BIN`, programın kendi başlattığı araçlar için Radiance'ın `bin`
klasörünü gösterebilir. Bu ayar kabuğunuzu hazırlamaz: aşağıdaki elle yazılan
`oconv`, `rpict`, `ra_ppm` komutları için araçlar `PATH` üzerinde olmalıdır.
Elle çalıştırırken `RAYPATH`, `.` ve Radiance `lib` klasörünü işletim sisteminin
yol ayıracıyla birleştirmelidir. Program kendi çocuk süreçlerinin ortamında
başa `.` ve bulunabiliyorsa dağıtımın `lib` klasörünü ekler, sizin `RAYPATH`
değerinizi ve diğer girdileri korur. Bu araçlar pakete dahil değildir.
Ayar dosyası varsa salt okunur olarak `radiance_bin` ve `raypath` için okunur:
`CTLUX_DATA_DIR` altında `ayarlar/ayar.json`, bu değişken verilmemişse kaynak
kökünün (`core/` klasörünü içeren klasör) altında `ayarlar/ayar.json`.
Ortamdaki `RADIANCE_BIN` bu dosyadaki `radiance_bin` değerinden önce gelir.

Komutları `Turkish/source` klasöründen çalıştırın:

```sh
export PYTHONDONTWRITEBYTECODE=1
python3 -m core input.ls10 output
# Aynı önbelleksiz kullanım:
python3 -B -m core input.evo output-evo
# Aydınlatma ürünleri:
python3 -B -m core products input.evo products-output
# Çeviri sonucunda yaşlı gözlemci için kamaşma:
python3 -B -m core glare output glare-output --vp 1 2 1.2 --vd 1 0 0.3 --age 75
```

Türkçe pakette Türkçe adlar da takma ad olarak kabul edilir: `products` yerine
`urunler`, `glare` yerine `kamasma`, `--age` yerine `--yas`, `--quality` yerine
`--kalite` (`taslak`, `orta` veya `final`), `--eye-pigmentation` yerine
`--goz-rengi`, `--source-limit-mib` yerine `--kaynak-siniri-mib` ve
`--triangle-limit` yerine `--ucgen-siniri`.

Çıktı klasörü yeni veya boş, üst klasörü mevcut olmalıdır, yolunda sembolik bağ kabul edilmez.
Kaynak değişmez. Geçici dosyalar çıktının altında oluşturulur ve başarıda/hata
durumunda temizlenir. Proje kasası, sunucu veya arayüz açılmaz. Python'un `-m`
yükleyicisi, giriş kodu başlamadan önce bytecode önbelleği yazabilir, bu yazımı
da önlemek için `-B` veya yukarıdaki ortam değişkeni gerekir.

Çeviri çıktıları: `scene.rad`, `geometri.rad`, `malzeme.rad`, `isiklar.rad`, `gorunum.vf`,
`scene.ctlux.json`, `RAPOR.txt`. Fotometri varsa `isik/` içinde IES ve Radiance
dağılım dosyaları da bulunur. `scene.rad` üç RAD bileşenini zaten içerir,
aynı geometriyi iki kez yüklemeyin. Radiance'ı çıktı klasöründe çalıştırın:
göreli dağılım yolları, çıktı klasörü bütün olarak taşındığında da çözülür.

`RAPOR.txt` okunanları, kısmi kalanları ve varsayılanları, atlananları ve
sınırları ayrı listeler. Çıkış kodu 0, kullanılabilir çıktı üretildiğini söyler,
aktarım yine kısmi olabilir. Bakış otomatik kurulduğu ve kaynak kamera
aktarılmadığı için çeviri raporu her zaman `Durum: kısmi` gösterir.
Desteklenmeyen/okunamayan giriş sıfırdan farklı
kod ve anlaşılır hata verir. Çıktı güvenle oluşturulabildiyse dönüşüm hatası
için de rapor bırakılır.
Ctrl+C kısa iptal iletisi ve 130, SIGTERM ise 143 çıkış kodu verir.
İkisi de sahipli çocuk süreçleri kapatır, geçici ve kısmen teslim edilmiş dosyaları siler.
Çıktı klasörü oluşturulmuşsa içinde açık bir iptal raporu kalır.
Sonuçlar çıktıya taşınırken hata olursa bu koşunun taşıdığı dosyalar da geri
alınır, `RAPOR.txt` hata kaydıyla kalır ve çıkış kodu 1 olur. Bu kural üç
komutta da geçerlidir.


Çeviride `--source-limit-mib N` ve `--triangle-limit N` pozitif tam sayı alır,
varsayılanlar 1024 MiB ve 3.000.000 üçgendir. Örneğin
`python3 -B -m core input.ls10 output --source-limit-mib 2048 --triangle-limit 6000000`.
Büyük sahne çok bellek ve disk ister. Kaynak kotası CTScene kurucusunun kayıtlı
OBJ/RAD, MTL ve doku dosyalarının toplamıdır, sıkıştırılmış girdi boyutu veya
süreç RAM sınırı değildir. Üçgen kotası kurucudan son Radiance doğrulamasına
kadar aynı çağrı parametresi olarak taşınır. Kota aşımında miktar ve yükseltme
seçeneği hata metninde, kullanılan sınırlar `RAPOR.txt` içinde yazılır,
örnekleme yapılmaz. Aynı araç uyarısı raporda dosya sayısı ve ilk üç dosyayla
birleştirilir, farklı metinler korunur.

## Aydınlatma ürünleri

Komut, elinizdeki bir projede kullanılan fotometriyi, o projeyi Radiance'ta
yeniden kurmak için listeler ve yazar. Tek girdi dosyası alır, klasör ya da
ikinci girdi reddedilir.

`python3 -B -m core products <input> <output_directory>`

Girdi: DIALux `.evo`, üretici fotometri paketi `.zip`, `.gldf`, `.ldt` veya `.ies`.
Çıktı klasörüne şunlar yazılır:

- Her ürün için bir IES dosyası
- `URUNLER.txt`: okunaklı tablo. Sütunlar: ad, üretici, lümen, ışın açısı,
  tepe kandela, C düzlemi sayısı, gama açısı sayısı, sahnedeki kullanım sayısı,
  IES dosyasının adı. Altında reddedilen ürünler ve nedenleri bulunur
- `URUNLER.json`: aynı bilgi, program için (`urunler` ve `reddedilen` listeleri).
  Birimler: `lumen` lm, `isin_acisi` derece (ilk C düzleminde eksenden yarı tepeye
  ölçülen açının iki katı, bkz. Sınırlar), `tepe_kandela` cd.
- `RAPOR.txt`: çeviriyle aynı dört başlık

Dosya adları: EVO ürünleri `proje_<girdi>_<tip>_<açı>d_<lümen>lm_u<kayıt>.ies`
biçimindedir, `<girdi>` girdi dosyasının adından türer (küçük harf, en çok 16
karakter). ZIP içindeki IES üyeleri, ışın açısı veya lümen okunabiliyorsa özgün adlarına
`_<açı>d_<lümen>lm` künyesini alır. ZIP içindeki LDT üyeleri aynı adla `.ies` olur. GLDF üyeleri,
ZIP içindeki GLDF'ninkiler de, `<gldf adı>_<üye adı>.ies` olur. Tek IES özgün adını korur,
tek LDT kendi adıyla `.ies` uzantısını alır. Ad doluysa `_2`, `_3` eklenir. Çıkarma var
olan dosyanın üstüne yazmaz, farklı klasörde ya da farklı GLDF'de aynı adı taşıyan
üyeler ayrı kalır.

Burada ve çeviri sonucunun `isik/` klasöründe yazılan her IES dosyası, `TILT=`
satırından önce, LM-63-2002'nin kullanıcı anahtar sözcüğü `[_NOTICE]` ile ASCII
şu notu taşır:

```text
[_NOTICE] Photometric data belongs to its owner, normally the luminaire
[MORE] manufacturer. Written from a lighting project file to rebuild
[MORE] that project.
```

Paketten ya da tek dosyadan (ZIP, GLDF, IES, LDT) gelen dosyada ikinci cümle
`Copied from a file supplied by the user.` olur. Kaynak dosyadaki anahtar
sözcükler korunur. EVO'dan yazılan IES dosyaları LM-63-2002'nin zorunlu tuttuğu
dört anahtar sözcüğü nötr değerle taşır: `[TEST]`, `[TESTLAB]` ve `[ISSUEDATE]`
`not available`, `[MANUFAC]` `not read from the project record` olur.
`URUNLER.txt` ve `RAPOR.txt`, başlıktan hemen sonra şu notla başlar: “Fotometri
verisi sahibine, çoğunlukla armatür üreticisine aittir. Bu dosyalar yalnız bu
projeyi hesap için yeniden kurmak amacıyla yazıldı.” Çeviri raporu bu notu
yalnız fotometri dosyası teslim ettiğinde taşır, hata ya da iptal raporu hiç
taşımaz.

Kurallar:

- Lümen yoksa sayı uydurulmaz. Ürün, neden olarak eksik lümen yazılıp reddedilir
  ve IES yazılmaz. Mutlak fotometrili IES (lümen alanı -1), `[_LUMENS]` alanında
  pozitif ve sonlu toplam lümen beyanı varsa kabul edilir, yoksa lümensiz sayılır.
  Lümen kandela tablosundan hesaplanmaz. LDT dönüşümü mutlak kandelayı korur ve
  kaynak lamba akısını `[_LUMENS]` alanına yazar, çıkan IES tekrar `products`
  komutuna verilebilir
- Üretilen EVO ve LDT başlıkları ASCII metindir, `[TEST]`, `[TESTLAB]`,
  `[ISSUEDATE]` ve `[MANUFAC]` içerir. LDT firma ve tarihi kaynaktan alınır
  (boşsa `not available`), adlar ASCII biçimine indirgenir. Bilinmeyen CCT
  yazılmaz. `[_SOURCE]` EULUMDAT dönüşümünü belirtir, yan yana duran
  `[_NOTICE]` ve `[MORE]` sahiplik satırlarından ayrıdır
- Negatif, sonlu olmayan ya da eksik kandela tablosu olan ürün reddedilir.
  Neden `RAPOR.txt` ve `URUNLER.txt` içine yazılır, diğer ürünler sürer
- Aynı ürün bir kez yazılır. EVO'da sahnede kaç kez kullanıldığı sayılır.
  Pakette bayt bayt aynı içerikli IES bir kez yazılır ve raporda aynı içerik diye
  belirtilir, dosyalar ada göre değil, okunduktan sonra içeriğe göre karşılaştırılır
- Hiç IES yazılamazsa çıkış kodu 1 olur, klasörde yalnız `RAPOR.txt` kalır
- Çıktı klasörü kuralları, kaynak değişmezliği ve Ctrl+C/SIGTERM davranışı
  çeviriyle aynıdır

Sınırlar:

- Tablodaki ışın açısı, ilk C düzleminde şiddetin tepe değerinin yarısına ilk
  düştüğü eksen açısının iki katıdır. Dosya adındaki açı künyesi mevcut kurala
  göre tablo değerini yeniden ikiye katlar
- EVO ürün kaydından üretici adı okunmaz, EVO'dan yazılan IES dosyalarındaki
  `[MANUFAC]` satırı bunu belirtir. LDT üretici bilgisi varsa korunur
- Fotometri dosyası tek başına sahne değildir, kullanım sayısı boş kalır

Fotometri verisi sahibine, çoğunlukla armatür üreticisine aittir. Bu depo
hiçbir ürün dosyası içermez.

## Yaşa göre kamaşma

`python3 -B -m core glare <scene_directory> <output_directory> [--vp X Y Z] [--vd DX DY DZ] [--age N] [--quality draft|medium|final] [--eye-pigmentation P]`

Sahne klasörü bir çeviri sonucudur: `scene.rad`, `scene.ctlux.json` ve `--vp`
ya da `--vd` eksikse `gorunum.vf`, eksik değer `gorunum.vf` dosyasından alınır.
`--age` gözlemcinin yaşıdır. 1 ile 120 arasında tam sayı alır,
varsayılanı 75. `--quality` Radiance kalitesini ve
kare balıkgözü boyutunu seçer: `draft` 400, `medium` 800 (varsayılan), `final`
1200 piksel. Yukarı vektörü +Z'dir (tam yukarı ya da aşağı bakışta +Y). Sahne
klasörü yalnız okunur, octree ve HDR görüntüler çıktının içindeki geçici
klasörde durur ve silinir. Süre ışık sayısı ve kaliteyle artar: yüzlerce
ışıklı bir sahnede `taslak` kalite de birkaç dakika sürebilir.

Çıktı klasörüne yalnız şu dosyalar yazılır:

- `KAMASMA.txt`: üç bölüm. Özet: bakış noktası ve yönü, kaynak sayısı, geçerlilik
  aralığı dışında kalan kaynak sayısı ve evalglare kaynaklı DGP ve UGR değerleri.
  Arka plan parlaklığı 0 iken evalglare UGR için -99
  yazar. UGR bu durumda tanımsızdır, ham metinle birlikte tanımsız diye gösterilir,
  JSON null saklar ve rapor durumu kısmi olur. ugp gibi sonlu olmayan diğer
  isteğe bağlı evalglare özet alanları da null saklanır. Kaynak tablosu: sıra, cisim adı, Radiance adı,
  parlaklık, katı açı, derece cinsinden açı, gözdeki aydınlık, örtü
  parlaklığına katkı, aralıkta mı, katkıya göre azalan sırada. Yaş tablosu: 20,
  40, 50, 60, 70, 80 ve `--age` için çarpan ve örtü parlaklığı (cd/m²)
- `KAMASMA.json`: aynı veriler, program için.
  Birimler: `vp` metre, `vd` birimsiz yön vektörü (yalnız yönü önemlidir,
  birim uzunluğa getirilmez), `parlaklik` cd/m², `kati_aci` sr, `aci` derece,
  `aydinlik` lx, `katki`, `ortu_parlakligi` ve `genel_ortu_parlakligi` cd/m².
  DGP 0 ile 1 arasında birimsiz bir değer, UGR ise birimsiz bir indistir.
- `goz.png`: ton eşlemeli balıkgözü görüntü. `isaretli.png`: evalglare'in
  kaynakları işaretlediği kontrol görüntüsü
- `RAPOR.txt`: çeviriyle aynı bölümler

Formül (CIE 146, Stiles ve Holladay):

```text
Lv  = toplam(10 * E_i / teta_i^2) * (1 + (yas / 70)^4)
E_i = L_i * omega_i * cos(teta_i)
```

`L_i` (cd/m²) ve `omega_i` (sr) evalglare kaynak satırından gelir. `teta_i`
bakış yönü ile kaynak yönü arasındaki açıdır (derece). Formül [CIE 146:2002](https://www.cie.co.at/publications/cie-collection-glare-2002)
kaynağına göre 1 < teta < 30 derece için geçerlidir. Aralık dışındaki kaynak toplama girmez, bu kaynakların
sayısı `KAMASMA.txt` ve `RAPOR.txt` içine yazılır. Yaş çarpanları: 20 için 1.01,
40 için 1.11, 50 için 1.26, 60 için 1.54, 70 için 2.00, 80 için 2.71. Katkı sütunu yaş
çarpanı öncesidir.

evalglare listesindeki her kaynak hesaba girer, armatür de olur, ışığı yansıtan
yüzey de. Böylece yansıyan ışık kendiliğinden içindedir. Her kaynak için gözden kaynağın yönüne bir ışın atılır (`rtrace -oms`).
Işının ilk çarptığı yüzey, kaynağa temsili bir ad verir. Bu işlem geniş bir
kaynağın bütün piksellerini tek tek tanımlamaz. Işık (`lN`) CTScene ışık adıyla,
üçgen (`yuzN`) CTScene malzeme adıyla etiketlenir.

Uydurma değer yok: evalglare başarısızsa, çıktısı kesik ya da tutarsızsa (sütun
sayısı yanlış kaynak satırı, başlıktaki sayıyla tutmayan kaynak listesi, eksik
DGP ya da UGR) veya bir kaynak cisimle eşlenemezse komut 1 ile biter ve yalnız
`RAPOR.txt` kalır. Kamaşma kaynağı olmayan bakışta sonuç sıfır kaynak ve sıfır
örtü parlaklığıdır. evalglare uyarıları `RAPOR.txt` içinde kısmi bölümüne
girer. `!` Radiance'ta kabuk komutunu işaretlediği için, yorum ve metin
içinde de olsa herhangi bir yerinde `!` bulunan `scene.rad` reddedilir.
`isik/` içindeki sembolik bağlar ile aygıt veya adlandırılmış boru gibi özel
dosyalar da reddedilir. Çıktı
klasörü kuralları, kaynak değişmezliği ve Ctrl+C/SIGTERM davranışı çeviriyle
aynıdır, aşağıdaki çizim sırası kilidi geçerlidir.

`RAPOR.txt` şu üç sınırı her zaman yazar:

- Malzemeler mat varsayılır, parlak yüzeyden aynasal yansıma bu sahnede oluşmaz
- EVO kaynaklı armatürlerde ışık çıkış açıklığının boyutu yer tutucudur,
  armatürün kendi parlaklığı ve ondan türetilen DGP, UGR ve örtü parlaklığı bu
  yüzden güvenilir değildir
- Formül 1 < açı < 30 derece koşulunda geçerlidir. Dışarıdaki kaynaklar toplama girmez

Ayrıca yüzeyde cisim adının malzeme adı olduğunu ve sahnede gök ya da gün ışığı
bulunmadığını yazar. Işık çıkış boyutu sıfır olan IES ışığı `ies2rad` ile 0,5 mm
yarıçaplı küre olur, sentetik IES sahnesinde 2 metreden bakışta kaynak olarak
bulunmadı.


Otomatik `gorunum.vf` çizim içindir, kamaşma için göz konumu odanın içinde
seçilmelidir. `--vp` ve `--vd` birlikte verilmezse rapor bunu hatırlatır.
evalglare'in stdout ve stderr bildirimleri kısmi bölümüne alınır, düşük dikey
aydınlık ve DGP bildirimleri Türkçeye çevrilir, bilinmeyen metin korunur.
Kaynak başlığındaki sayı sıfırsa yer tutucu satırın arka plan değerleri
kaynak sayılmaz, özet denetimi sürer, iki toplam da sıfır olur.

İkinci toplam CIE genel görme engelleyici kamaşma denklemidir:

```text
Lv_genel = toplam(E * (10/teta^3 + (5/teta^2 + 0.1*p/teta)*(1+(yas/62.5)^4) + 0.0025*p))
0.1 < teta < 100 derece
```

`--eye-pigmentation p` 0 ile 1.2 arasında sonlu katsayı alır: siyah 0, kahverengi
0.5, açık 1.0, çok açık 1.2. Varsayılan 0.5, NODD çalışmasının orta örneğidir,
evrensel nüfus ortalaması değildir. Denklem
[NIST makalesi, denklem 4](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=917534)
ve [NODD, denklem 1 ve Tablo 11](https://doi.org/10.1364/AO.54.001564)
ile karşılaştırıldı, açı aralığı [CIE açıklamasıyla](https://www.cie.co.at/publications/cie-collection-glare-2002)
uyumludur. Yaş tablosunda Stiles ve Holladay ile genel örtü parlaklığı ayrı sütunlardır,
her aralığın dışındaki kaynak sayısı ve p değeri ayrıca yazılır.

Genel toplam yalnız bulunan kaynakları kapsar. 180 derece balıkgözü görüntü
bakış yönünden 90 derece ötesini örneklemez, denklemin 100 derece sınırı bu
kamera sınırını kaldırmaz. Göz aydınlığı `L * omega * cos(teta)` olarak
hesaplanır, negatif aydınlık genel denklemde kabul edilmez. 1 ile 30
derece toplamı gerçek odalarda birçok tavan armatürünü dışarıda
bırakabilir. Sıfır toplam tek başına kamaşma olmadığı anlamına gelmez.

## Yürütme Mantığı

Kamaşmada iki akümülatör sıfırdan başlar. Kaynak sayısı N ise açı ve göz aydınlığı N kez hesaplanır, Stiles ve Holladay toplamı yalnız 1 ile 30 derece katkılarını alır. Yaş tablosundaki her yaş için ikinci akümülatör, 0.1 < açı < 100 koşulunu sağlayan kaynakları toplar. Her adımın sonunda akümülatör, o ana kadar geçerli bulunan katkıların toplamıdır. N kaynak bitince döngü durur, N sıfırsa iki toplam da sıfır kalır.

`girdi → okuyucu → geometri/ışık/uyarı → CTScene doğrulaması → Radiance yazıcısı → rapor`

Başlangıçta geometri, ışık ve uyarı listeleri boştur. Komut ilk tetikleyicidir.
Okuyucu her adımda bir kayıt çözer, listeye ekler veya neden atlandığını bildirir.
Sahne kurucu yüzleri dolaşır, indisleri ve kaynak boyutunu kontrol eder,
üretilen üçgen sayacı her üçgende artar. Kaynak bittiğinde döngü sona erer.
Sert sınır aşılırsa hata verir, geometriyi sessiz örneklemez.

Yazıcı her üçgene bir Radiance polygon, her açık ışığa uygun bir temsil üretir.
IES dağılımı gerçek `ies2rad` ve `xform` ile işlenir. Fotometrisiz ışığın gücü
yaklaşık olarak raporlanır. Sonuç dosyaları geçici alandan çıktı köküne alınır.
Kaynak değişmezliği, indis doğrulaması ve açık kayıp raporu akışın temel
koşullarıdır. Kullanıcı sonucu dosyalarda ve `RAPOR.txt` içinde görür.

`products` akışı: `girdi → fotometri okuyucusu → ürün başına doğrulama → IES + tablo → rapor`.
EVO'da ürünler STEP kayıtlarından okunur, paket, GLDF, LDT ve IES girdilerinde
her fotometri dosyası bir üründür. Geçersiz ürün ayrı raporlanır, akış durmaz.

`glare` akışı: `sahne klasörü → oconv → 180° balıkgözü (rpict) → evalglare -d → kaynak başına rtrace → CTScene adı → formül → dosyalar → rapor`.

## Radiance araçlarıyla ilişki

- Tek komut çeviricisi fotometri için `ies2rad`, ışık yerleşimi için `xform` çağırır
- Sahne polygonlarını kendi yazar, bu komutta `obj2rad`, `obj2mesh`, `oconv`, `rpict` çalıştırmaz
- `products`, EVO fotometrisi için `ies2rad` çağırır, diğer girdilerde Radiance gerekmez
- Çizim API'si mesh önbelleği için `obj2mesh`, polygon/alternatif geometri için `obj2rad` kullanır
- `oconv` octree üretir, `rpict` görüntüyü çizer, `rpiece` çizimi böler, `vwrays` native görüntü boyutunu denetler
- `sahne_derle` gök metnini çağırandan alır, çekirdek gök metni üretmez. `xform` geometri/ışık dönüşümü içindir
- `getbbox` kamera çerçevesi için sınırları bulur, `ra_tiff` dokuları Radiance görüntüsüne çevirir
- `pfilt` elle pozlama, `pcond` otomatik ton eşleme, `ra_ppm` PNG yazımına piksel üretimi içindir
- `pextrem` uç değerleri okur (siyah görüntü kontrolü dahil), `falsecolor` etiketli analiz renklerini üretir
- `glare`, `oconv` çalıştırır, çizim API'siyle görüntüyü çizer, kaynaklar, DGP ve UGR için `evalglare -d -c`, cisim adları için `rtrace -oms` çağırır
- `pvalue` yalnız testlerde kullanılır, çeviri ve `products` `rtrace` çağırmaz

## Teşekkür ve kaynaklar

Bu program başkalarının emeği üzerinde durur. Onların araçlarını çağırır ve
yayımladıkları denklemleri kullanır.

- Radiance: Greg Ward yazdı, Lawrence Berkeley National Laboratory'de
  geliştirildi. Ward, G. J. (1994). The RADIANCE Lighting Simulation and
  Rendering System. SIGGRAPH '94. Denenen sürüm 6.0.2
- evalglare: Jan Wienold yazdı, Radiance ile dağıtılıyor. Denenen sürüm 3.06.
  DGP: Wienold, J. ve Christoffersen, J. (2006). Evaluation methods and
  development of a new glare prediction model for daylight environments with
  the use of CCD cameras. Energy and Buildings, 38(7)
- Görme engelleyici kamaşma: CIE 146:2002, CIE Equations for Disability
  Glare. Vos, J. J. (2003). On the cause of disability glare and its
  dependence on glare angle, age and ocular pigmentation. Clinical and
  Experimental Optometry, 86(6)
- Genel denklem Uchida ve Ohno (NIST, denklem 4) ile Williamson ve McLin
  (2015), Nominal ocular dazzle distance (NODD), Applied Optics, 54(7)
  çalışmalarıyla karşılaştırıldı. Bağlantılar yukarıdaki kamaşma bölümündedir
- UGR: CIE 117-1995, Discomfort Glare in Interior Lighting
- Fotometri dosya biçimi: ANSI/IESNA LM-63-02
- İki dosya biçimi üzerine daha önce yapılmış açık çalışmalar yöntem
  notlarında, her yöntem deposunun `RELATED_WORK.md` dosyasında anılır:
  [ctlux-ls10](https://github.com/JamesAugustus/ctlux-ls10) ve
  [ctlux-evo](https://github.com/JamesAugustus/ctlux-evo)

## Biçimler ve kanıtları

Yazar macOS üzerinde de denemeler yaptı. Lumion/LS10 için tam görüntülü deneme
Linux üzerinde yapıldı. Lumion ışıklarının bulunması kısmi kaldı, macOS
denemeleri Lumion'un tam görüntüleme akışını kapsamıyor. Bu gözlemler yazarın
bildirimidir. Aşağıdaki otomatik Linux testleri ayrıca belirtilir

DAE ve FBX yalnız import biçimleridir. DAE, FBX ve GLB, Assimp ile import
edilir. Export API'si GLB, OBJ, USDA ve Radiance yazar. GLB için hem import
hem export desteklenir. DAE ve FBX export işlemi uygulanmadı.

“Çalışıyor”, belirtilen sentetik örneğin geçtiği anlamındadır, bütün sürüm ve
dosyaları eksiksiz destekleme iddiası değildir. Kanıt yolları `tests/` altındadır.

| Girdi | Durum ve kapsam | Test kanıtı |
| --- | --- | --- |
| Lumion `.ls10` | Kısmi: mesh ve ışık, malzeme/doku/sürüm anlamı sınırlı | `test_core.CommandTest.test_cli_ls10`, `test_ls10_preservation`, `test_ls10_surface` |
| DIALux `.evo` | Kısmi: STEP oda/yerleşim, oda yüzleri odanın içine bakar, kurtarılabilen fotometri, kapalı geometri eksik | `test_core.CommandTest.test_cli_evo`, `test_evo_components`, `test_evo_identity`, `test_evo_photometry_validation` |
| OBJ | Denenen statik geometri çalışıyor, metre/Z-up varsayılır | `test_core.CommandTest.test_cli_obj`, `test_scene_model` |
| ASCII/binary STL, 3DS | Assimp üzerinden sentetik üçgenler çalışıyor, tam malzeme iddiası yok | `test_core.CommandTest.test_cli_assimp_formats` |
| GLB, glTF, DAE, ASCII/binary FBX | Assimp üzerinden statik üçgenler çalışıyor, animasyon/ışık/tam malzeme ağı kurtarılmıyor | `test_core.CommandTest.test_cli_assimp_formats`, `test_assimp_bridge` |
| USDA | usd-core ile denenen statik mesh/ışık alt kümesi çalışıyor | `test_core.CommandTest.test_cli_usda`, `test_usd_native` |
| USDC, USDZ | Okuyucu test edildi, tek komut yolu ayrıca ölçülmedi | `test_usd_native.USDTest.test_usdc_binary`, `.test_usdz_paket` |
| IES | Denenen açı tabloları çalışıyor, eksik/fazla kandela hata | `test_core.CommandTest.test_cli_ies_ldt_use_real_photometry`, `SinirTest.test_ies_incomplete_and_extra_candela_are_errors` |
| LDT | Desteklenen simetri ve tek lamba seti çalışıyor, desteklenmeyen eğim/simetri hata | `test_ldt_translation`, `test_core.CommandTest.test_cli_ies_ldt_use_real_photometry` |
| `products`: EVO, fotometri ZIP'i, GLDF, LDT | Sentetik ürünler, lümensiz, negatif kandelalı ve kopya ürünler raporlanır, aynı adlı üyeler ayrı kalır, sahiplik notu, nötr EVO başlığı, `ies2rad` ışığı değişmez, yalnız tek girdi | `test_core.UrunlerTest` |
| `glare`: çeviri sonucu | Tavanında tek ışık olan sentetik oda, formül, yaş çarpanları, aralık sayımı, evalglare çözümü, cisim adları, hata ve iptal | `test_glare` |
| Diğer Lumion sürümleri, LSF, L3D | Okuyucu yok | Yok |
| 3DM, IFC, ABC, PLY, X3D, LWO, OFF, MS3D, BLEND, AC | Okuyucu yok | Yok |

## Sınırlar

- EVO varsayılan armatür sınırı 4000. Kesilirse okunan, sınır ve atlanan adetleri
  bildirilir. Konumu çözülemeyen armatür ve kesik STEP kaydı da raporlanır.
  Mobilya kutularının ayrı 4000 sınırı ve atlanan gömülü FBX parçaları da bildirilir. STEP yardımcısı tam doğrulayıcı değildir
- Lumion'da alan eksik veya etiket farklıysa varsayılan değerler kullanılır:
  tür 0, koni 1.0, en/boy 1.0, beyaz. Hangi ışığın hangi alanı olduğu yazılır.
  Renk ve güç fiziksel kalibrasyon değildir
- CTScene kurucusu varsayılan olarak toplam 1024 MiB kayıtlı kaynak, 3.000.000 üçgen ve yüz başına
  4096 köşe sınırını uygular. Bu, genel bir arşiv açma/bellek kotası değildir
- Radiance mat temel renk kullanır. Doku, cam/metal shader anlamı ve yumuşak köşe
  normali uygulanmadığında raporlanır. Bazı özgün mesh alanları CTScene'de kalır,
  malzeme dosyası başvurusu, dokunun çıktı paketine alındığı anlamına gelmez
- Tek IES/LDT girdisi orijinde -Z yönünde yalnız ışık sahnesi üretir, oda eklenmez.
  Işıksız geometriye gök eklenmez. Kamera otomatik çerçevelenir
- IES ışığı nadiri ışık yönüne, C0 düzlemi dünya +X ekseninin bu yöne dik
  izdüşümüne bakacak biçimde döndürülür, X boyunca yatay ışıkta dünya +Y
  kullanılır. Aşağı bakan ışıkta C0, `ies2rad` çıktısındaki gibi +X'te kalır.
  Kaynak dosyadaki ışık ekseni etrafındaki dönüş kullanılmaz, simetrik olmayan
  dağılım bu yüzden yanlış yöne bakabilir
- Proje içindeki göreli dosya adı hiçbir platformda `a:` gibi bir sürücü önekiyle
  başlayamaz, bu yüzden `a:b.txt` adlı dosya reddedilir. `scan 12:30.obj` gibi iki
  nokta içeren diğer adlar Windows dışında kabul edilir
- Tam native geri dönüş, bütün ürün sürümleri, fiziksel kalibrasyon, kötü niyetli
  arşivlerin kaynak tüketimi ve farklı platformlardaki native koşular doğrulanmadı

## İleride yapılacaklar

Bu maddeler açıktır. Sentetik test verisiyle gelen katkılar memnuniyetle karşılanır

- EVO armatürünün ışık ekseni etrafındaki dönüşü. C0 düzlemi şu an sabit bir
  kurala göre yerleşir (bkz. Sınırlar). EVO eleman çerçevesinin x ekseni C0 yönü
  için güçlü bir aday, ama planda döndürülmüş asimetrik bir armatürle DIALux
  sonuçlarına karşı doğrulanması gerekir. `motor.fotometri_donusu` bir C0
  eksenini zaten kabul eder, içe aktarıcının ve sahne modelinin bunu taşıması
  gerekir. ctlux-evo yöntem notlarının 4. bölümüne ve Sınırlar kısmına bakın
- Boşluk içeren `TMPDIR` ile `falsecolor`. Varsa kabuk için güvenli `/tmp` veya
  `/var/tmp` kullanılır. İkisi de olmayan sistemler, örneğin Windows, kapsanmaz
- Işın açısı adlandırması. Ürün tablosu ilk C düzlemindeki yarı tepe açısının iki
  katını gösterir. Dosya adı künyesi ile spot, downlight ve geniş sınıflandırması
  tablo değerini yeniden ikiye katlar. Bu değerler ışın açısının alışılmış tam
  koni anlamıyla uyumlu hale getirilmeli
- Düşey açıları 90 dereceden başlayan yukarı ışık tablolarında ışın açısı 180 çıkar
- Bir EVO ekipman kaydında birden çok `LampTypeChannel` kaydı varsa son değer
  kullanılır. Toplanmaları gerekip gerekmediği açıktır
- macOS ve Windows üzerinde native koşular
- Görüş alanının sol ve sağ yarıları arasındaki görsel denge. Yazar, sol tarafı
  karanlık, sağ tarafı aydınlık olan ve sağda göz hizasında parlak bir kaynak
  bulunan bir görüşün rahatsız edici olabildiğini gözlemledi. Böyle bir
  dengesizliğin görsel rahatsızlık ya da migrenle ilişkisi bir araştırma
  sorusudur. Şu an yalnız teorik olarak araştırılıyor, çekirdek bunu henüz
  hesaplamaz

## Gelecek arayüz için herkese açık çağrılar

Önerilen bağımsız girişler:
`core.__main__.cevir(girdi_yolu, cikti_klasoru)`,
`core.__main__.urunler(girdi_yolu, cikti_klasoru)` ve
`core.__main__.kamasma(sahne_klasoru, cikti_klasoru, vp=None, vd=None, yas=75, kalite='orta', goz_rengi=0.5)`.

| İş | Adlar |
| --- | --- |
| Biçim tanıma | `core.format_detector.analiz`, `uyarilar` |
| Dönüştürme | `core.importer.cevir`, `lumion_armaturler`, `evo_armaturler`, `evo_odalar`, `usd_kopru_calistir` |
| Aydınlatma ürünleri | `core.importer.evo_ies`, `evo_fotometri_yaz`, `zip_kutuphane`, `gldf_ac`, `ldt_ies` (`core.engine.ldt_ies` aynı işlevdir), `core.photometry.ies_kaynagi`, `core.tools.ies_analysis` |
| Nötr sahne | `core.scene_model.scene_from_project`, `validate_scene`, `core.evo_identity.zenginlestir` |
| Yazım | `core.scene_writers.export_scene` (`radiance`/`rad`, `glb`, `obj`, `usda`), `export_radiance`, `default_camera` |
| Kamera | `core.scene_camera.camera_normalize`, `radiance_view`, `core.engine.oto_cerceve` |
| Radiance çizimi | `core.engine.ortam_hazirla`, `sahne_derle`, `gorunum`, `gorunum_serbest`, `render`, `hdr_to_png`, `falsecolor_png`, `pextrem` |
| İlerleme | `core.render_progress.is_akisi`, `akis_durumu`, `durum` |
| Kamaşma | `core.glare.evalglare_coz`, `ortu_parlakligi`, `yas_carpani`, `yas_tablosu`, `cisim_adlari`, `metin` |

`export_radiance`, fotometri için `asset_resolver(göreli_ies_yolu) -> {'data': bytes}`
çözücüsü kabul eder. `sahne_derle(proje_dir, proje, gok_metni)` gök metnini
çağırandan alır. `akis_durumu(islem_id)` yalnız `is_akisi(islem_id)` bağlamında
çalışan işi okur. `evo_ies(evo, proje_dir, reddedilen=liste)` geçersiz ürünü
hata yerine listeye yazar, liste verilmezse hata yükseltir.

Çekirdek kendiliğinden klasör açmaz. Ürün işlevleri hedef klasörü parametre
olarak alır: `zip_kutuphane(kaynak, hedef)`, `gldf_ac(kaynak, hedef)`,
`evo_fotometri_yaz(proje_dir, hedef, proje_ad)`. Varsayılan kütüphane, gelen kutusu
veya proje kökü yoktur. Çizim API'si çağıranın verdiği proje klasöründe `_cache`
altına yazar, `oto_cerceve`, projede hedef yoksa hedefi hesaplar ve varsayılan
olarak `proje.json` yazar, `kaydet=False` ile yazmaz. Çizim sırası kilidi `/tmp/ctlux-render-<kullanıcı no>.lock` dosyasıdır
(`CTLUX_RENDER_LOCK` ile değişir). `CTLUX_RENDER_WORKERS=1` çizimi seri yapar. Modülleri import etmek klasör oluşturmaz ve
arayüz modülü yüklemez.

## Sentetik örnek ve çizim

```sh
python3 -B tests/synthetic.py /tmp/ctlux-inputs
python3 -B -m core /tmp/ctlux-inputs/ucgen.obj /tmp/ctlux-output
(
cd /tmp/ctlux-output
oconv scene.rad > scene.oct
rpict -vf gorunum.vf -x 320 -y 240 -ab 0 -ad 64 -as 16 -av .2 .2 .2 scene.oct > draft.hdr
ra_ppm -g 2.2 draft.hdr > draft.ppm
)
```

Buradaki `-av`, ışıksız üçgeni görmek için taslak ortam katkısıdır, aydınlatma
ölçümü değildir. PNG dönüşümü için `engine.hdr_to_png` vardır. Bu sürümde
`engine` üzerinden sentetik bir zemin ve IES ışığı derlenip 64×43 piksel çizildi,
görüntü boş değildi.

Sentetik ürün örneği (`Turkish/source` klasöründen):

```sh
python3 -B - <<'PYTHON'
import sys
sys.path.insert(0, 'tests')
import synthetic
synthetic.fotometri_zip('/tmp/ctlux-package.zip')
PYTHON
python3 -B -m core products /tmp/ctlux-package.zip /tmp/ctlux-products
```

Sentetik kamaşma örneği: tavanında tek ışık olan 4 x 5 x 3 metrelik oda, yandan
bakış. Işık yaklaşık 21,8 derecede bulunur ve `tavan lambası` adıyla eşlenir.

```sh
python3 -B - <<'PYTHON'
import sys
sys.path.insert(0, 'tests')
import synthetic
synthetic.kamasma_odasi('/tmp/ctlux-room.evo')
PYTHON
python3 -B -m core /tmp/ctlux-room.evo /tmp/ctlux-room
python3 -B -m core glare /tmp/ctlux-room /tmp/ctlux-glare --vp 0.8 2.5 1.2 --vd 1 0 0.6 --quality draft
```

Test komutu: `python3 -B -m unittest discover -s tests -v`.
22 test modülü bulunur. İsteğe bağlı bağımlılık yoksa test açık gerekçeyle
atlanır, Radiance ya da evalglare yoksa kamaşma komutu testleri atlanır.
`test_core`, bütün çekirdek alt dizinlerini yasak import açısından tarar,
çeviriyi ve `products` komutunu yazma sınırı kontrolüyle çalıştırır,
`test_glare` aynısını `glare` için yapar.

Yöntem notları:

- “LS10 project file grammar: method notes”:
  https://github.com/JamesAugustus/ctlux-ls10
- “EVO lighting layout recovery for Radiance: method notes”:
  https://github.com/JamesAugustus/ctlux-evo

Lisans: MIT Lisansı veya Apache Lisansı 2.0 seçeneklerinden birini seçebilirsiniz
(`MIT OR Apache-2.0`). Bkz. [LICENSE](../LICENSE), [LICENSE-MIT](../LICENSE-MIT)
ve [LICENSE-APACHE](../LICENSE-APACHE). İki seçenek de ticari kullanıma,
değiştirmeye ve kaynak kodu kapalı ürünlerde dağıtıma izin verir.
İkisi de kaynak kodunuzu yayımlamanızı gerektirmez.

MIT seçildiğinde yazılımın bütün kopyalarında veya önemli bölümlerinde telif ve
izin bildirimi korunur. Apache 2.0 seçildiğinde yeniden dağıtımda lisansın bir
kopyası verilir, değiştirilen dosyalara belirgin değişiklik bildirimleri eklenir,
dağıtılan kaynakta ilgili telif, patent, marka ve atıf bildirimleri korunur.
İlgili [NOTICE](../NOTICE) atıfları bölüm 4'ün izin verdiği biçimlerden biriyle
sunulur. Apache NOTICE yükümlülüğü Apache 2.0 seçildiğinde geçerlidir.
MIT seçimi Apache koşullarını getirmez.

İki seçenek de kullanıcı arayüzünde isim gösterilmesini veya akademik atfı
zorunlu kılmaz. [CITATION.cff](../CITATION.cff) ile atıf yapılması takdir edilir
ve isteğe bağlıdır. Bu lisanslar özgün CTLux kodunu ve belgelerini kapsar.
Harici araçlar ve kullanıcının sağladığı proje veya fotometri verileri kendi
koşullarına tabidir.

Geliştirme notu: tasarım, matematik ve dosya biçimlerinin çözümlemesi yazara
aittir. Yazar genellikle C ile çalışır. Yapay zekâ araçları Python kodunun
yazılmasında ve gözden geçirilmesinde, belgelerin düzenli tutulmasında yardımcı oldu.
