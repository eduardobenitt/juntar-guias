"""
Lógica do fluxo: renomear comprovantes, casar com as guias e juntar os PDFs.

Este módulo NÃO sabe nada de web: recebe arquivos (nome + bytes) e devolve
o resultado. Assim dá para testar sozinho e trocar a interface (ou o destino,
ex.: S3) sem mexer aqui.
"""
import csv
import io
import re
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import pypdfium2
import zxingcpp
from pypdf import PdfReader, PdfWriter


# =======================
#  PADRÕES DE EXTRAÇÃO
# =======================
# Linha digitável "colada": 47 (boleto bancário) ou 48 (arrecadação) dígitos.
PADRAO_LINHA = re.compile(r"\b\d{47,48}\b")

# Linha digitável formatada, ex.: "23790.12345 60000.000000 ..." ou
# "89630000019-8 39681210103-0 ..." -> removemos pontos/espaços/hífens depois
# e conferimos se sobraram 47 ou 48 dígitos.
PADRAO_LINHA_FORMATADA = re.compile(r"\d[\d. \-]{45,70}\d")

PADRAO_DESCRICAO = re.compile(
    r"(?:descrição|descricao)\s*[:\-]?\s*(\d{3,15})",
    re.IGNORECASE,
)

# Número da SPF no nome da guia, ex.: "SPF 280738.pdf" -> "280738"
PADRAO_NUMERO_NOME = re.compile(r"(\d{3,15})")

# Abaixo disso, analisa os arquivos um a um (abrir processos não compensa)
MINIMO_PARA_PARALELO = 20


# =======================
#  ESTRUTURAS DE DADOS
# =======================
@dataclass
class Arquivo:
    nome: str
    dados: bytes


@dataclass
class Comprovante:
    nome_original: str
    dados: bytes
    tipo: str = ""        # "SPF" ou "LINHA"
    chave: str = ""       # número da SPF ou a linha digitável
    codigo_barras: str = ""  # só para tipo LINHA: a linha convertida em código de barras
    novo_nome: str = ""


@dataclass
class Guia:
    nome: str
    dados: bytes
    spf: str | None = None
    codigos_barras: list[str] = field(default_factory=list)


@dataclass
class Juntado:
    nome: str
    dados: bytes
    guia: str
    comprovantes: list[str]


@dataclass
class Pendente:
    nome: str
    dados: bytes
    tipo: str             # "Guia" ou "Comprovante"
    motivo: str


@dataclass
class Resultado:
    juntados: list[Juntado] = field(default_factory=list)
    pendentes: list[Pendente] = field(default_factory=list)
    renomeados: list[tuple[str, str, str]] = field(default_factory=list)  # (original, novo, tipo)


# =======================
#  LEITURA DE PDF
# =======================
def ler_texto(pdf: pypdfium2.PdfDocument) -> str:
    """Extrai o texto de todas as páginas do PDF.
    Usa o pypdfium2 (motor de PDF do Chrome): em guias cheias de imagens ele é
    ~100x mais rápido que o pdfplumber, que analisa cada objeto da página."""
    partes = []
    for pagina in pdf:
        texto_pagina = pagina.get_textpage()
        partes.append(texto_pagina.get_text_range())
        texto_pagina.close()
        pagina.close()
    return "\n".join(partes)


def encontrar_linhas_digitaveis(texto: str) -> list[str]:
    """Devolve as linhas digitáveis do texto, na ordem em que aparecem, sem repetir."""
    encontradas = list(PADRAO_LINHA.findall(texto))
    for trecho in PADRAO_LINHA_FORMATADA.findall(texto):
        digitos = re.sub(r"\D", "", trecho)
        if len(digitos) in (47, 48):
            encontradas.append(digitos)
    return list(dict.fromkeys(encontradas))  # remove duplicadas mantendo a ordem


def encontrar_descricao(texto: str) -> str | None:
    m = PADRAO_DESCRICAO.search(texto)
    return m.group(1) if m else None


# =======================
#  CÓDIGO DE BARRAS
# =======================
def linha_para_codigo_barras(linha: str) -> str:
    """Converte a linha digitável (47 ou 48 dígitos) no código de barras (44 dígitos).
    É o mesmo número, só que reorganizado e sem os dígitos verificadores dos blocos.
    Assim dá para comparar o comprovante com o código de barras lido da guia."""
    if len(linha) == 48:
        # Arrecadação/convênio (começa com 8): 4 blocos de 11 dígitos + 1 verificador cada
        return linha[0:11] + linha[12:23] + linha[24:35] + linha[36:47]
    # Boleto bancário (47): banco+moeda | DV geral | vencimento+valor | campo livre (3 partes)
    return linha[0:4] + linha[32] + linha[33:47] + linha[4:9] + linha[10:20] + linha[21:31]


def ler_codigos_de_barras(pdf: pypdfium2.PdfDocument) -> list[str]:
    """Lê os códigos de barras desenhados no PDF (funciona mesmo em guia que é só imagem).
    Cada página vira uma imagem; se não achar nada, tenta de novo com resolução maior."""
    for escala in (3, 4):
        codigos = []
        for pagina in pdf:
            imagem = pagina.render(scale=escala).to_pil()
            for r in zxingcpp.read_barcodes(imagem, formats=zxingcpp.BarcodeFormat.ITF):
                if len(r.text) == 44 and r.text.isdigit():
                    codigos.append(r.text)
            pagina.close()
        if codigos:
            return list(dict.fromkeys(codigos))
    return []


# =======================
#  IDENTIFICAÇÃO
# =======================
def identificar_comprovante(arq: Arquivo) -> Comprovante:
    """Mesma regra do script antigo: 1º linha digitável, 2º número da 'Descrição'."""
    comp = Comprovante(nome_original=arq.nome, dados=arq.dados)
    pdf = pypdfium2.PdfDocument(arq.dados)
    try:
        texto = ler_texto(pdf)
    finally:
        pdf.close()

    linhas = encontrar_linhas_digitaveis(texto)
    if linhas:
        comp.tipo, comp.chave = "LINHA", linhas[0]
        comp.codigo_barras = linha_para_codigo_barras(linhas[0])
        return comp

    numero = encontrar_descricao(texto)
    if numero:
        comp.tipo, comp.chave = "SPF", numero
    return comp


def identificar_guia(arq: Arquivo) -> Guia:
    """A guia é identificada pelo número da SPF no nome do arquivo.
    Também guardamos o(s) código(s) de barras dela, para casar com comprovantes
    identificados pela linha digitável:
      1º tenta achar a linha digitável no TEXTO do PDF (rápido);
      2º se não houver texto (guia escaneada/imagem), LÊ O CÓDIGO DE BARRAS da imagem."""
    guia = Guia(nome=arq.nome, dados=arq.dados)

    m = PADRAO_NUMERO_NOME.search(arq.nome.rsplit(".", 1)[0])
    if m:
        guia.spf = m.group(1)

    try:
        pdf = pypdfium2.PdfDocument(arq.dados)  # abre UMA vez e usa para texto e imagem
    except Exception:
        return guia  # PDF ilegível: ainda dá para casar pelo número da SPF no nome

    try:
        linhas = encontrar_linhas_digitaveis(ler_texto(pdf))
        guia.codigos_barras = [linha_para_codigo_barras(l) for l in linhas]
        if not guia.codigos_barras:
            guia.codigos_barras = ler_codigos_de_barras(pdf)
    except Exception:
        pass  # mantém o que já tiver sido encontrado
    finally:
        pdf.close()
    return guia


def _analisar(tarefa: tuple[str, Arquivo]):
    """Roda em um processo separado (ver processar). Identifica uma guia ou um
    comprovante e devolve (objeto, erro). Os bytes do PDF NÃO voltam para o
    processo principal (ele já os tem): copiar 400 PDFs de volta seria desperdício."""
    tipo, arq = tarefa
    try:
        obj = identificar_guia(arq) if tipo == "guia" else identificar_comprovante(arq)
        obj.dados = b""
        return obj, None
    except Exception as e:
        return None, str(e)


def analisar_todos(guias_arqs: list[Arquivo], comprovantes_arqs: list[Arquivo]):
    """Identifica todos os arquivos. Lotes grandes usam vários processos ao mesmo
    tempo (um por núcleo do processador); lotes pequenos rodam direto, porque
    abrir os processos custa ~1 segundo."""
    tarefas = [("guia", a) for a in guias_arqs] + [("comprovante", a) for a in comprovantes_arqs]
    if len(tarefas) < MINIMO_PARA_PARALELO:
        resultados = [_analisar(t) for t in tarefas]
    else:
        with ProcessPoolExecutor() as pool:
            resultados = list(pool.map(_analisar, tarefas, chunksize=4))

    # Devolve os bytes a cada objeto (eles ficaram só no processo principal)
    for (_, arq), (obj, _) in zip(tarefas, resultados):
        if obj is not None:
            obj.dados = arq.dados
    n = len(guias_arqs)
    return resultados[:n], resultados[n:]


# =======================
#  NOMES E JUNÇÃO
# =======================
def nomear_comprovantes(comprovantes: list[Comprovante]) -> None:
    """Define o novo nome de cada comprovante, sem repetir nomes no lote.
    SPF -> 'SPF 280738-1.pdf', 'SPF 280738-2.pdf'...
    LINHA -> '<linha>.pdf', '<linha>-2.pdf'..."""
    usados: set[str] = set()
    for comp in comprovantes:
        if comp.tipo == "SPF":
            i = 1
            while f"SPF {comp.chave}-{i}.pdf" in usados:
                i += 1
            comp.novo_nome = f"SPF {comp.chave}-{i}.pdf"
        else:
            nome, i = f"{comp.chave}.pdf", 2
            while nome in usados:
                nome = f"{comp.chave}-{i}.pdf"
                i += 1
            comp.novo_nome = nome
        usados.add(comp.novo_nome)


def juntar_pdfs(partes: list[bytes]) -> bytes:
    """Concatena os PDFs na ordem recebida (guia primeiro, depois comprovantes)."""
    escritor = PdfWriter()
    for dados in partes:
        escritor.append(PdfReader(io.BytesIO(dados)))
    saida = io.BytesIO()
    escritor.write(saida)
    return saida.getvalue()


# =======================
#  FLUXO COMPLETO
# =======================
def processar(guias_arqs: list[Arquivo], comprovantes_arqs: list[Arquivo]) -> Resultado:
    resultado = Resultado()
    analises_guias, analises_comprovantes = analisar_todos(guias_arqs, comprovantes_arqs)

    # 1) Comprovantes (o que não der para ler/identificar vira pendente)
    comprovantes: list[Comprovante] = []
    for arq, (comp, erro) in zip(comprovantes_arqs, analises_comprovantes):
        if erro:
            resultado.pendentes.append(Pendente(arq.nome, arq.dados, "Comprovante", f"Erro ao ler o PDF: {erro}"))
            continue
        if not comp.chave:
            resultado.pendentes.append(Pendente(arq.nome, arq.dados, "Comprovante", "Sem linha digitável e sem número na 'Descrição'"))
            continue
        comprovantes.append(comp)

    nomear_comprovantes(comprovantes)
    for comp in comprovantes:
        resultado.renomeados.append((comp.nome_original, comp.novo_nome, comp.tipo))

    # 2) Identifica as guias e monta "índices" para achar a guia rapidamente
    guias = [
        guia if guia is not None else Guia(nome=arq.nome, dados=arq.dados)
        for arq, (guia, _) in zip(guias_arqs, analises_guias)
    ]
    por_spf: dict[str, list[Guia]] = {}
    por_barras: dict[str, list[Guia]] = {}
    for guia in guias:
        if guia.spf:
            por_spf.setdefault(guia.spf, []).append(guia)
        for codigo in guia.codigos_barras:
            por_barras.setdefault(codigo, []).append(guia)

    # 3) Casa cada comprovante com a sua guia
    #    SPF   -> pelo número da SPF (nome da guia)
    #    LINHA -> pelo código de barras (linha do comprovante convertida x barras da guia)
    comprovantes_da_guia: dict[str, list[Comprovante]] = {}  # nome da guia -> comprovantes
    for comp in comprovantes:
        if comp.tipo == "SPF":
            candidatas = por_spf.get(comp.chave, [])
        else:
            candidatas = por_barras.get(comp.codigo_barras, [])
        if len(candidatas) == 1:
            comprovantes_da_guia.setdefault(candidatas[0].nome, []).append(comp)
        elif not candidatas:
            resultado.pendentes.append(Pendente(comp.novo_nome, comp.dados, "Comprovante", "Nenhuma guia correspondente"))
        else:
            nomes = ", ".join(g.nome for g in candidatas)
            resultado.pendentes.append(Pendente(comp.novo_nome, comp.dados, "Comprovante", f"Mais de uma guia possível: {nomes}"))

    # 4) Junta guia + comprovante(s); guias sem comprovante ficam pendentes
    for guia in guias:
        comps = comprovantes_da_guia.get(guia.nome)
        if not comps:
            motivo = "Nenhum comprovante correspondente" if guia.spf else "Nome sem número de SPF"
            resultado.pendentes.append(Pendente(guia.nome, guia.dados, "Guia", motivo))
            continue
        try:
            dados = juntar_pdfs([guia.dados] + [c.dados for c in comps])
        except Exception as e:
            resultado.pendentes.append(Pendente(guia.nome, guia.dados, "Guia", f"Erro ao juntar: {e}"))
            for c in comps:
                resultado.pendentes.append(Pendente(c.novo_nome, c.dados, "Comprovante", "Erro ao juntar com a guia"))
            continue
        resultado.juntados.append(Juntado(guia.nome, dados, guia.nome, [c.novo_nome for c in comps]))

    return resultado


# =======================
#  SAÍDA (ZIP)
# =======================
def gerar_zip(resultado: Resultado) -> bytes:
    """ZIP com: juntados/, pendentes/ e relatorio.csv (abre direto no Excel)."""
    buffer = io.BytesIO()
    # ZIP_STORED = sem compressão: PDF já é comprimido por dentro, então
    # comprimir de novo gasta muito tempo de CPU e quase não reduz o tamanho.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for j in resultado.juntados:
            zf.writestr(f"juntados/{j.nome}", j.dados)
        for p in resultado.pendentes:
            zf.writestr(f"pendentes/{p.nome}", p.dados)

        relatorio = io.StringIO()
        w = csv.writer(relatorio, delimiter=";")
        w.writerow(["Situação", "Arquivo", "Detalhe"])
        for j in resultado.juntados:
            w.writerow(["Juntado", j.nome, "Comprovantes: " + ", ".join(j.comprovantes)])
        for p in resultado.pendentes:
            w.writerow([f"Pendente ({p.tipo})", p.nome, p.motivo])
        # utf-8-sig: o Excel reconhece acentos corretamente
        zf.writestr("relatorio.csv", relatorio.getvalue().encode("utf-8-sig"))
    return buffer.getvalue()
