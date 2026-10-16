# Third-party notes

The core runtime uses Python standard-library components and browser APIs. Optional development checks use Node.js. Optional deterministic indexing uses Tree-sitter and tree-sitter-language-pack; these packages and their grammars retain their upstream licenses and are not bundled as binary dependencies in the source release. Install them in a dedicated worker environment and retain their notices when redistributing binaries.

No Magpie or Zed source files, brand assets or fonts are bundled. Their repository documentation informed the presentation and contribution structure: https://github.com/yetone/magpie and https://github.com/zed-industries/zed . This is not an endorsement or affiliation.

Codex is an external optional integration and is not redistributed. The released UI uses available system fonts; no proprietary font binaries are included.
