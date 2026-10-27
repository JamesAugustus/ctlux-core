# CTLux Core

[Türkçe](tr/README.md)

CTLux converts supported Lumion 10 and DIALux evo project records to Radiance scenes. It can also extract product photometry and analyse age-related glare. The conversion scope and unsupported records are described in the [technical guide](md/CTLUX_CORE.md).

## Read and run

[Technical guide](md/CTLUX_CORE.md) covers installation, commands, formats, API examples and limitations. The implementation is in [core/](core/) and its synthetic tests are in [tests/](tests/). Format studies are maintained in [ctlux-evo](https://github.com/JamesAugustus/ctlux-evo) and [ctlux-ls10](https://github.com/JamesAugustus/ctlux-ls10).

From the repository root:

```sh
python3 -B -m core --help
python3 -B -m core input.ls10 output
python3 -B -m unittest discover -s tests -v
```

Python 3.9 or later is required. Radiance and optional format tools are listed in the guide. Tests requiring unavailable external tools are skipped. Each language has its own source and test tree and must run in a separate Python process.

## License

Original content is available under [MIT](LICENSE-MIT) or [Apache 2.0](LICENSE-APACHE), at your choice. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [CITATION.cff](CITATION.cff).

This independent project is not affiliated with Act-3D B.V. or DIAL GmbH. Lumion and DIALux identify the source applications and their file formats.
