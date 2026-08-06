/**
 * RN 0.76 pins fmt 9.x, whose consteval format-string checking fails to
 * compile under Xcode 26's stricter C++20 ("call to consteval function ...
 * is not a constant expression"). FMT_USE_CONSTEVAL=0 downgrades the check
 * to constexpr — same behavior fmt uses on older toolchains.
 *
 * The generated ios/ project is disposable (CNG), so the fix lives here as a
 * config plugin: every `expo prebuild` / EAS build re-injects it into the
 * Podfile's post_install. Drop this plugin once the app moves to an Expo SDK
 * whose React Native bundles fmt >= 10.
 */

const { withDangerousMod } = require("expo/config-plugins");
const fs = require("fs");
const path = require("path");

const MARKER = "FMT_USE_CONSTEVAL=0";
const SNIPPET = `
    # Injected by plugins/withFmtConsteval.js — see that file for the story.
    installer.pods_project.targets.each do |target|
      target.build_configurations.each do |config|
        defs = Array(config.build_settings['GCC_PREPROCESSOR_DEFINITIONS'] || ['$(inherited)'])
        defs << '${MARKER}' unless defs.include?('${MARKER}')
        config.build_settings['GCC_PREPROCESSOR_DEFINITIONS'] = defs
      end
    end
    # fmt 11.0.x ignores an external FMT_USE_CONSTEVAL (no #ifndef guard —
    # fixed upstream in 11.1). Patch the guard into the pod's base.h so the
    # define above actually takes effect.
    fmt_base = File.join(installer.sandbox.root, 'fmt/include/fmt/base.h')
    if File.exist?(fmt_base)
      fmt_src = File.read(fmt_base)
      unless fmt_src.include?('ifndef FMT_USE_CONSTEVAL')
        fmt_src.sub!("#if !defined(__cpp_lib_is_constant_evaluated)\\n",
                     "#ifndef FMT_USE_CONSTEVAL\\n#if !defined(__cpp_lib_is_constant_evaluated)\\n")
        fmt_src.sub!("#endif\\n#if FMT_USE_CONSTEVAL\\n",
                     "#endif\\n#endif\\n#if FMT_USE_CONSTEVAL\\n")
        File.chmod(0644, fmt_base)
        File.write(fmt_base, fmt_src)
      end
    end
`;

module.exports = function withFmtConsteval(config) {
  return withDangerousMod(config, [
    "ios",
    (modConfig) => {
      const podfile = path.join(modConfig.modRequest.platformProjectRoot, "Podfile");
      let contents = fs.readFileSync(podfile, "utf8");
      if (!contents.includes(MARKER)) {
        // Append inside the post_install block, just before its closing `end`
        // (the first end at two-space indent after the block opens).
        contents = contents.replace(
          /(post_install do \|installer\|[\s\S]*?)(\n {2}end\n)/,
          `$1\n${SNIPPET}$2`,
        );
        fs.writeFileSync(podfile, contents);
      }
      return modConfig;
    },
  ]);
};
