# Third-party notes

The core runtime uses Python standard-library components and browser APIs. Optional development checks use Node.js. Optional deterministic indexing uses Tree-sitter and tree-sitter-language-pack; these packages and their grammars retain their upstream licenses and are not bundled as binary dependencies in the source release. Install them in a dedicated worker environment and retain their notices when redistributing binaries.

No Magpie or Zed source files, brand assets or fonts are bundled. Their repository documentation informed the presentation and contribution structure: https://github.com/yetone/magpie and https://github.com/zed-industries/zed . This is not an endorsement or affiliation.

Codex is an external optional integration and is not redistributed. The released UI bundles the full Noto Sans SC variable font as WOFF2, without subsetting, under SIL Open Font License 1.1. See [font license](../web/fonts/OFL.txt) and [font notice](../web/fonts/NOTICE.md). Font conversion preserves the original character map; the font is not relicensed under MIT.

Additional presentation references and concrete choices are documented in [repository design](repository-design.md).
