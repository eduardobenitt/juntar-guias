"""
Aplicação web: o financeiro abre no navegador, envia guias + comprovantes
e recebe o relatório e um ZIP com os arquivos juntados e pendentes.

Rodar:  .venv\\Scripts\\python.exe app.py   ->  http://<servidor>:8000
"""
import re
import time
import uuid
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from nucleo import Arquivo, gerar_zip, processar

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # limite de 500 MB por envio

PASTA_SAIDA = Path(__file__).parent / "saida"
PASTA_SAIDA.mkdir(exist_ok=True)
HORAS_PARA_APAGAR = 24


def limpar_saidas_antigas():
    """Apaga ZIPs com mais de 24h para a pasta não crescer para sempre."""
    limite = time.time() - HORAS_PARA_APAGAR * 3600
    for arq in PASTA_SAIDA.glob("*.zip"):
        if arq.stat().st_mtime < limite:
            arq.unlink(missing_ok=True)


def ler_envio(campo: str) -> list[Arquivo]:
    """Lê os PDFs enviados num campo do formulário (ignora o que não for .pdf)."""
    arquivos = []
    for f in request.files.getlist(campo):
        nome = Path(f.filename or "").name  # .name evita caminhos tipo "../../x.pdf"
        if nome.lower().endswith(".pdf"):
            arquivos.append(Arquivo(nome=nome, dados=f.read()))
    return arquivos


@app.get("/")
def inicio():
    return render_template("index.html")


@app.post("/processar")
def processar_envio():
    guias = ler_envio("guias")
    comprovantes = ler_envio("comprovantes")
    if not guias or not comprovantes:
        return render_template("index.html", erro="Envie pelo menos uma guia e um comprovante em PDF.")

    resultado = processar(guias, comprovantes)

    limpar_saidas_antigas()
    id_execucao = uuid.uuid4().hex
    (PASTA_SAIDA / f"{id_execucao}.zip").write_bytes(gerar_zip(resultado))

    return render_template(
        "resultado.html",
        r=resultado,
        id_execucao=id_execucao,
        total_guias=len(guias),
        total_comprovantes=len(comprovantes),
    )


@app.get("/baixar/<id_execucao>")
def baixar(id_execucao: str):
    # Aceita só o formato do uuid (32 caracteres hexadecimais) -> ninguém pede outro arquivo do servidor
    if not re.fullmatch(r"[0-9a-f]{32}", id_execucao):
        abort(404)
    caminho = PASTA_SAIDA / f"{id_execucao}.zip"
    if not caminho.exists():
        return redirect(url_for("inicio"))
    nome = time.strftime("juntados_%Y-%m-%d_%H%M.zip")
    return send_file(caminho, as_attachment=True, download_name=nome)


if __name__ == "__main__":
    # waitress: servidor de produção simples e que funciona bem no Windows
    from waitress import serve

    print("Aplicação no ar em http://localhost:8000  (Ctrl+C para parar)")
    serve(app, host="0.0.0.0", port=8000, max_request_body_size=app.config["MAX_CONTENT_LENGTH"])
