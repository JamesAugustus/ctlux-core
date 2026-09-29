# Türkçe kaynak kod ve testler

Bu klasörde CTLux Core kaynak kodunun ve sentetik testlerinin Türkçe sürümü bulunur.
Ana İngilizce sürüm depo kökündeki `core/` ve `tests/` klasörlerindedir

## Çalıştırma

Komutları bu `Turkish/source` klasöründen çalıştırın

```bash
python3 -B -m core --help
python3 -B -m unittest discover -s tests -v
```

Python bu konumda yanındaki `core` paketini kullanır.
İngilizce ve Türkçe sürümlerin alan adları farklı olabilir.
Her sürümün testleri kendi kaynak koduyla çalıştırılır.
Her dil için ayrı bir Python süreci başlatın.
`PYTHONPATH` ve `sys.path` içinde yalnız seçtiğiniz dilin kaynak kökünü kullanın

## Numuneler

Testler küçük geometri, ışık ve dosya kayıtlarını kodla üretir.
Örnek ürün adları ve kimlikleri sentetiktir.
Bazı testler hatalı dosyaları, boş alanları, Türkçe karakterleri ve geçersiz yolları özellikle kullanır

Testlerin geçmesi, sınanan durumların beklenen sonucu verdiğini gösterir.
Gerçek projelerin bütün ayrıntılarının aktarılacağını veya ölçümlerin resmî olarak onaylandığını göstermez

## Lisans

Kaynak kod ve testler depo kökündeki [MIT](../../LICENSE-MIT) veya [Apache 2.0](../../LICENSE-APACHE) lisansı seçenekleriyle sunulur
