import js from '@eslint/js';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'dist-e2e-*', 'node_modules', 'test-results', 'playwright-report', 'src/api/schema.d.ts'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: { globals: globals.browser },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    files: ['src/**/*.tsx'],
    ...jsxA11y.flatConfigs.strict,
  },
  {
    files: ['*.{js,ts,mjs}', 'scripts/**/*.mjs', 'e2e/**/*.ts'],
    languageOptions: { globals: globals.node },
  },
);
