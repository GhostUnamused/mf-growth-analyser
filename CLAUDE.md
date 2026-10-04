# MF Growth Analyser

Streamlit app (`app.py`) deployed on Streamlit Community Cloud from `main`.

## Workflow
- After making and verifying changes, commit and push them to `main` directly
  (the owner wants every change live without a separate merge step).
- Run `python -c "import ast; ast.parse(open('app.py').read())"` and, where the
  network allows, `streamlit run app.py` before pushing.
- Never commit secrets; the NewsAPI key lives in `.streamlit/secrets.toml`
  (git-ignored) or the Streamlit Cloud Secrets settings.
