"""Tienda esqueleto: tienda pública + panel de admin con cuentas.

Configuración por variables de entorno (en Render > Environment):
  DATABASE_URL  link de conexión de Neon (si falta, usa un archivo local que Render BORRA)
  SECRET_KEY    texto largo al azar (protege las sesiones)
  Opcional, fotos en almacenamiento S3 (Neon Object Storage u otro):
  S3_BUCKET, S3_ENDPOINT, S3_KEY, S3_SECRET, S3_PUBLIC_URL, S3_REGION
  Sin S3, las fotos se guardan dentro de la base de datos.
"""
import io, os, re, secrets, time, uuid
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from PIL import Image, ImageOps
from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary,
                        MetaData, String, Table, Text, create_engine, delete, func,
                        insert, select, update)
from werkzeug.security import check_password_hash, generate_password_hash

# Busca los .html en la carpeta "templates"; si no existe (subidos sueltos a GitHub), los busca junto a app.py
_AQUI = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder="templates" if os.path.isdir(os.path.join(_AQUI, "templates")) else ".")
app.secret_key = os.environ.get("SECRET_KEY") or "solo-para-pruebas-locales"
app.config.update(MAX_CONTENT_LENGTH=40 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---------- Base de datos ----------
def _db_url():
    u = os.environ.get("DATABASE_URL", "sqlite:///tienda.db")
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    if u.startswith("postgresql://"):
        u = "postgresql+psycopg://" + u[len("postgresql://"):]
    return u

DB_URL = _db_url()
DB_LOCAL = DB_URL.startswith("sqlite")
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)
md = MetaData()

usuarios = Table("usuarios", md,
    Column("id", Integer, primary_key=True),
    Column("celular", String(20), unique=True, nullable=False),
    Column("nombre", String(80)),
    Column("clave", String(255), nullable=False),
    Column("rol", String(12), nullable=False, default="cliente"),  # dueno | asociado | ayudante | cliente
    Column("creado", DateTime, server_default=func.now()))

productos = Table("productos", md,
    Column("id", Integer, primary_key=True),
    Column("nombre", String(120), nullable=False),
    Column("descripcion", Text, default=""),
    Column("precio", Integer, nullable=False),
    Column("precio_anterior", Integer),
    Column("categoria", String(60), default=""),
    Column("youtube", String(11)),
    Column("activo", Boolean, default=True),
    Column("creado", DateTime, server_default=func.now()))

imagenes = Table("imagenes", md,
    Column("id", Integer, primary_key=True),
    Column("producto_id", Integer, ForeignKey("productos.id", ondelete="CASCADE")),
    Column("tipo", String(10), default="producto"),  # producto | logo
    Column("orden", Integer, default=0),
    Column("ancho", Integer), Column("alto", Integer),
    Column("grande", LargeBinary), Column("chica", LargeBinary),
    Column("clave_s3", String(60)))

config = Table("config", md,
    Column("clave", String(40), primary_key=True),
    Column("valor", Text))

md.create_all(engine)

CONFIG_BASE = {
    "nombre": "Mi Tienda", "subtitulo": "Tienda online",
    "portada_titulo": "Bienvenidos", "portada_texto": "Elegí en el catálogo y pedí por WhatsApp.",
    "color_principal": "#24273a", "color_acento": "#f2a516",
    "whatsapp": "", "instagram": "", "horario": "", "direccion": "",
}

def tienda_cfg():
    if "cfg" not in g:
        with engine.connect() as c:
            filas = dict(c.execute(select(config.c.clave, config.c.valor)).all())
        g.cfg = {**CONFIG_BASE, **filas}
    return g.cfg

def guardar_cfg(datos):
    with engine.begin() as c:
        for k, v in datos.items():
            if c.execute(select(config.c.clave).where(config.c.clave == k)).first():
                c.execute(update(config).where(config.c.clave == k).values(valor=v))
            else:
                c.execute(insert(config).values(clave=k, valor=v))

# ---------- Fotos ----------
S3 = None
if os.environ.get("S3_BUCKET"):
    import boto3
    from botocore.config import Config as BotoConfig
    S3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT"),
                      aws_access_key_id=os.environ.get("S3_KEY"),
                      aws_secret_access_key=os.environ.get("S3_SECRET"),
                      region_name=os.environ.get("S3_REGION", "us-east-1"),
                      config=BotoConfig(s3={"addressing_style": "path"}))
    S3_BUCKET = os.environ["S3_BUCKET"]
    S3_PUBLIC = os.environ.get("S3_PUBLIC_URL", "").rstrip("/")

def procesar_foto(archivo):
    """Devuelve (grande_webp, chica_webp, ancho, alto). Grande 2000 px para calidad, chica 600 px para la grilla."""
    im = Image.open(archivo)
    im.draft("RGB", (2000, 2000))  # en JPEG decodifica ya reducido: menos memoria y más rápido
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    gr = im.copy(); gr.thumbnail((2000, 2000), Image.LANCZOS)
    ch = im.copy(); ch.thumbnail((600, 600), Image.LANCZOS)
    bg, bc = io.BytesIO(), io.BytesIO()
    gr.save(bg, "WEBP", quality=85, method=4)
    ch.save(bc, "WEBP", quality=80, method=4)
    return bg.getvalue(), bc.getvalue(), gr.size[0], gr.size[1]

def guardar_foto(archivo, producto_id=None, tipo="producto", orden=0):
    grande, chica, w, h = procesar_foto(archivo)
    fila = dict(producto_id=producto_id, tipo=tipo, orden=orden, ancho=w, alto=h)
    if S3:
        clave = uuid.uuid4().hex
        for sufijo, datos in (("g", grande), ("c", chica)):
            S3.put_object(Bucket=S3_BUCKET, Key=f"{clave}-{sufijo}.webp", Body=datos,
                          ContentType="image/webp", CacheControl="public, max-age=31536000, immutable")
        fila["clave_s3"] = clave
    else:
        fila.update(grande=grande, chica=chica)
    with engine.begin() as c:
        return c.execute(insert(imagenes).values(**fila)).inserted_primary_key[0]

def borrar_fotos(condicion):
    with engine.begin() as c:
        if S3:
            for (clave,) in c.execute(select(imagenes.c.clave_s3).where(condicion)).all():
                if clave:
                    for s in ("g", "c"):
                        S3.delete_object(Bucket=S3_BUCKET, Key=f"{clave}-{s}.webp")
        c.execute(delete(imagenes).where(condicion))

@app.route("/img/<int:iid>/<any(g,c):t>")
def img(iid, t):
    col = imagenes.c.grande if t == "g" else imagenes.c.chica
    with engine.connect() as c:
        fila = c.execute(select(imagenes.c.clave_s3, col).where(imagenes.c.id == iid)).first()
    if not fila:
        abort(404)
    if fila[0]:
        return redirect(f"{S3_PUBLIC}/{fila[0]}-{t}.webp", 301)
    r = Response(fila[1], mimetype="image/webp")
    r.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # cada foto nueva tiene id nuevo
    return r

# ---------- Utilidades ----------
def yt_id(url):
    url = (url or "").strip()
    if not url:
        return None
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/))([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[A-Za-z0-9_-]{11}", url) else False

def solo_celular(s):
    d = re.sub(r"\D", "", s or "")
    return d if 8 <= len(d) <= 15 else None

def pesos(n):
    return "$" + f"{int(n):,}".replace(",", ".")

def tinta(hex_color):
    """Negro o blanco, el que se lea mejor sobre ese color."""
    h = (hex_color or "#000000").lstrip("#")
    try:
        r, gg, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"
    return "#1a1a1a" if (0.299 * r + 0.587 * gg + 0.114 * b) > 150 else "#ffffff"

def entero(s):
    try:
        return int(re.sub(r"[^\d]", "", s or "")) if (s or "").strip() else None
    except ValueError:
        return None

app.jinja_env.filters.update(pesos=pesos, tinta=tinta)

# ---------- Cuentas y seguridad ----------
JEFES = ("dueno", "asociado")             # manejan todo: tienda, cuentas y productos
EQUIPO = ("dueno", "asociado", "ayudante")  # pueden cargar productos

def usuario_actual():
    if "yo" not in g:
        g.yo = None
        if session.get("uid"):
            with engine.connect() as c:
                f = c.execute(select(usuarios).where(usuarios.c.id == session["uid"])).mappings().first()
            g.yo = dict(f) if f else None
    return g.yo

def requiere(*roles):
    def deco(fn):
        @wraps(fn)
        def envuelta(*a, **k):
            u = usuario_actual()
            if not u:
                return redirect(url_for("login", next=request.path))
            if u["rol"] not in roles:
                abort(403)
            return fn(*a, **k)
        return envuelta
    return deco

@app.before_request
def revisar_csrf():
    if request.method == "POST" and (not session.get("csrf") or request.form.get("csrf") != session["csrf"]):
        abort(400)

@app.context_processor
def contexto():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return dict(csrf=session["csrf"], yo=usuario_actual(), T=tienda_cfg(), DB_LOCAL=DB_LOCAL)

FALLOS = {}  # celular -> (intentos, hora del último)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        cel = solo_celular(request.form.get("celular"))
        n, t = FALLOS.get(cel, (0, 0))
        if n >= 5 and time.time() - t < 60:
            flash("Demasiados intentos. Esperá un minuto y probá de nuevo.")
            return render_template("login.html"), 429
        with engine.connect() as c:
            u = c.execute(select(usuarios).where(usuarios.c.celular == cel)).mappings().first() if cel else None
        if u and check_password_hash(u["clave"], request.form.get("clave", "")):
            FALLOS.pop(cel, None)
            session.clear(); session["uid"] = u["id"]
            destino = request.args.get("next", "")
            return redirect(destino if destino.startswith("/") and not destino.startswith("//") else url_for("admin"))
        FALLOS[cel] = (n + 1, time.time())
        flash("Celular o contraseña incorrectos.")
    return render_template("login.html")

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        f = request.form
        cel = solo_celular(f.get("celular"))
        if not cel:
            flash("Escribí el celular con código de área, solo números (8 a 15).")
        elif len(f.get("clave", "")) < 6:
            flash("La contraseña tiene que tener al menos 6 caracteres.")
        elif f.get("clave") != f.get("clave2"):
            flash("Las dos contraseñas no coinciden.")
        else:
            with engine.begin() as c:
                if c.execute(select(usuarios.c.id).where(usuarios.c.celular == cel)).first():
                    flash("Ese celular ya tiene cuenta. Entrá con tu contraseña.")
                    return render_template("registro.html")
                primero = c.execute(select(func.count()).select_from(usuarios)).scalar() == 0
                uid = c.execute(insert(usuarios).values(
                    celular=cel, nombre=f.get("nombre", "").strip()[:80],
                    clave=generate_password_hash(f["clave"]),
                    rol="dueno" if primero else "cliente")).inserted_primary_key[0]
            session.clear(); session["uid"] = uid
            if not primero:
                flash("Cuenta creada. Para administrar, pedile al dueño que te dé permiso.")
            return redirect(url_for("admin"))
    return render_template("registro.html")

@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("tienda"))

# ---------- Tienda pública ----------
def productos_publicos():
    with engine.connect() as c:
        prods = c.execute(select(productos).where(productos.c.activo == True)  # noqa: E712
                          .order_by(productos.c.categoria, productos.c.nombre)).mappings().all()
        fotos = c.execute(select(imagenes.c.id, imagenes.c.producto_id)
                          .where(imagenes.c.tipo == "producto").order_by(imagenes.c.orden, imagenes.c.id)).all()
    por_prod = {}
    for iid, pid in fotos:
        por_prod.setdefault(pid, []).append(iid)
    return [dict(id=p["id"], n=p["nombre"], d=p["descripcion"] or "", p=p["precio"],
                 old=p["precio_anterior"], cat=p["categoria"] or "Otros", yt=p["youtube"],
                 fotos=por_prod.get(p["id"], [])) for p in prods]

def logo_id():
    with engine.connect() as c:
        return c.execute(select(imagenes.c.id).where(imagenes.c.tipo == "logo")
                         .order_by(imagenes.c.id.desc())).scalar()

@app.route("/")
def tienda():
    return render_template("tienda.html", productos=productos_publicos(), logo=logo_id())

@app.route("/salud")
def salud():
    return "ok"

# ---------- Panel de admin ----------
@app.route("/admin")
def admin():
    u = usuario_actual()
    if not u:
        return redirect(url_for("login"))
    if u["rol"] == "cliente":
        return render_template("sin_permiso.html")
    return redirect(url_for("admin_productos"))

@app.route("/admin/productos")
@requiere(*EQUIPO)
def admin_productos():
    with engine.connect() as c:
        prods = c.execute(select(productos).order_by(productos.c.creado.desc(), productos.c.id.desc())).mappings().all()
        primera = {}
        for iid, pid in c.execute(select(imagenes.c.id, imagenes.c.producto_id)
                                  .where(imagenes.c.tipo == "producto").order_by(imagenes.c.orden.desc(), imagenes.c.id.desc())).all():
            primera[pid] = iid
    return render_template("admin_productos.html", prods=prods, primera=primera)

def categorias_existentes():
    with engine.connect() as c:
        return [r[0] for r in c.execute(select(productos.c.categoria).distinct()
                                        .where(productos.c.categoria != "")).all()]

@app.route("/admin/productos/nuevo", methods=["GET", "POST"])
@app.route("/admin/productos/<int:pid>", methods=["GET", "POST"])
@requiere(*EQUIPO)
def admin_producto(pid=None):
    if request.method == "POST":
        f = request.form
        datos = dict(nombre=f.get("nombre", "").strip()[:120], descripcion=f.get("descripcion", "").strip(),
                     precio=entero(f.get("precio")), precio_anterior=entero(f.get("precio_anterior")),
                     categoria=f.get("categoria", "").strip()[:60], activo=bool(f.get("activo")))
        yt = yt_id(f.get("youtube"))
        errores = []
        if not datos["nombre"]:
            errores.append("Falta el nombre.")
        if datos["precio"] is None:
            errores.append("Falta el precio (solo números).")
        if yt is False:
            errores.append("El link de YouTube no es válido. Copiá el link del video con el botón Compartir.")
        if errores:
            for e in errores:
                flash(e)
        else:
            datos["youtube"] = yt
            with engine.begin() as c:
                if pid:
                    c.execute(update(productos).where(productos.c.id == pid).values(**datos))
                else:
                    pid = c.execute(insert(productos).values(**datos)).inserted_primary_key[0]
                orden = (c.execute(select(func.max(imagenes.c.orden)).where(imagenes.c.producto_id == pid)).scalar() or 0)
            malas = 0
            for i, archivo in enumerate(request.files.getlist("fotos")):
                if archivo and archivo.filename:
                    try:
                        guardar_foto(archivo.stream, pid, orden=orden + i + 1)
                    except Exception:
                        malas += 1
            if malas:
                flash(f"{malas} archivo(s) no se pudieron leer como foto. Probá con JPG o PNG.")
            flash("Producto guardado.")
            return redirect(url_for("admin_producto", pid=pid))
    prod, fotos = None, []
    if pid:
        with engine.connect() as c:
            prod = c.execute(select(productos).where(productos.c.id == pid)).mappings().first()
            if not prod:
                abort(404)
            fotos = c.execute(select(imagenes.c.id, imagenes.c.ancho, imagenes.c.alto)
                              .where(imagenes.c.producto_id == pid).order_by(imagenes.c.orden, imagenes.c.id)).all()
    return render_template("admin_producto.html", prod=prod, fotos=fotos, cats=categorias_existentes())

@app.route("/admin/productos/<int:pid>/borrar", methods=["POST"])
@requiere(*EQUIPO)
def admin_borrar_producto(pid):
    borrar_fotos(imagenes.c.producto_id == pid)
    with engine.begin() as c:
        c.execute(delete(productos).where(productos.c.id == pid))
    flash("Producto borrado.")
    return redirect(url_for("admin_productos"))

@app.route("/admin/foto/<int:iid>/<any(borrar,primera):accion>", methods=["POST"])
@requiere(*EQUIPO)
def admin_foto(iid, accion):
    with engine.connect() as c:
        pid = c.execute(select(imagenes.c.producto_id).where(imagenes.c.id == iid)).scalar()
    if accion == "borrar":
        borrar_fotos(imagenes.c.id == iid)
    else:
        with engine.begin() as c:
            minimo = c.execute(select(func.min(imagenes.c.orden)).where(imagenes.c.producto_id == pid)).scalar() or 0
            c.execute(update(imagenes).where(imagenes.c.id == iid).values(orden=minimo - 1))
    return redirect(url_for("admin_producto", pid=pid))

@app.route("/admin/tienda", methods=["GET", "POST"])
@requiere(*JEFES)
def admin_tienda():
    if request.method == "POST":
        f = request.form
        datos = {k: f.get(k, "").strip()[:500] for k in CONFIG_BASE}
        for k in ("color_principal", "color_acento"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", datos[k]):
                datos[k] = CONFIG_BASE[k]
        datos["whatsapp"] = re.sub(r"\D", "", datos["whatsapp"])
        guardar_cfg(datos)
        logo = request.files.get("logo")
        if logo and logo.filename:
            try:
                guardar_foto(logo.stream, tipo="logo")
                borrar_fotos((imagenes.c.tipo == "logo") & (imagenes.c.id != logo_id()))
            except Exception:
                flash("El logo no se pudo leer como imagen. Probá con PNG o JPG.")
        flash("Datos de la tienda guardados.")
        return redirect(url_for("admin_tienda"))
    return render_template("admin_tienda.html", logo=logo_id())

@app.route("/admin/cuentas", methods=["GET", "POST"])
@requiere(*JEFES)
def admin_cuentas():
    if request.method == "POST":
        yo = usuario_actual()
        uid, rol = entero(request.form.get("uid")), request.form.get("rol")
        # El dueño puede dar o quitar "asociado". El asociado solo maneja ayudantes y clientes.
        puede_dar = ("asociado", "ayudante", "cliente") if yo["rol"] == "dueno" else ("ayudante", "cliente")
        intocables = ("dueno",) if yo["rol"] == "dueno" else ("dueno", "asociado")
        if rol in puede_dar and uid != yo["id"]:
            with engine.begin() as c:
                hecho = c.execute(update(usuarios).where((usuarios.c.id == uid) & usuarios.c.rol.notin_(intocables))
                                  .values(rol=rol)).rowcount
            flash("Permiso actualizado." if hecho else "No tenés permiso para cambiar esa cuenta.")
        else:
            flash("No tenés permiso para dar ese permiso.")
        return redirect(url_for("admin_cuentas"))
    with engine.connect() as c:
        lista = c.execute(select(usuarios.c.id, usuarios.c.nombre, usuarios.c.celular, usuarios.c.rol)
                          .order_by(usuarios.c.id)).mappings().all()
    return render_template("admin_cuentas.html", lista=lista)

@app.errorhandler(413)
def muy_grande(_):
    flash("Las fotos pesan demasiado juntas (máximo 40 MB por vez). Subilas de a pocas.")
    return redirect(request.referrer or url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True)
