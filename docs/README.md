# Гайд по сервису Site Checker (черновик, в работе)

- `Гайд_Site_Checker.md` — исходник гайда (правится здесь).
- `build_docx.js` — конвертер Markdown → styled .docx (стиль как в гайде OpenGar).
- `Гайд_Site_Checker.docx` — собранный документ (результат конвертера).

Пересобрать .docx после правок .md:
```
node docs/build_docx.js docs/Гайд_Site_Checker.md docs/Гайд_Site_Checker.docx
```
(нужен npm-пакет `docx`: `npm install docx`)
