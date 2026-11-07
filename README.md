# Auditoría SEO para Drupal

Herramientas para rastrear páginas, auditar sus metadatos y generar propuestas con
Ollama. La auditoría funciona en modo de prueba por defecto; solo `--apply`
escribe cambios en Drupal.

## Llevar el proyecto a otra máquina

### 1. Qué subir a GitHub

Desde esta carpeta, incluye:

- `audit.py`, `crawler.py`, `graph_store.py` y `test_audit.py`: código y pruebas.
- `requirements.txt`: dependencias de Python.
- `campos.json`, `grabacion_campos.json` e `instrucciones_seo.json`:
  configuración del modelo, selectores grabados e instrucciones.
- `resultado.json`: grafo de páginas de origen.
- `auditoria.json`: progreso, estados, valores y propuestas de la auditoría.
- `.gitignore` y este `README.md`.
- `resultado.html` y `auditoria.html` son visualizaciones opcionales; se pueden
  regenerar desde los JSON.

**Revisa los archivos antes de subirlos.** Los JSON y HTML pueden contener
información de páginas, contenido editorial y propuestas. Usa un repositorio
privado y súbelos únicamente si tienes autorización para almacenar esa información
en GitHub. Un repositorio privado no sustituye los permisos necesarios.

No subas `drupal_state/`: contiene el perfil del navegador, posiblemente con
cookies y credenciales de sesión. Tampoco subas `.venv/`, `__pycache__/` ni
`lib/`; el `.gitignore` actual los excluye. Las credenciales y los modelos de
Ollama no se transfieren con el repositorio. La máquina nueva requerirá iniciar
sesión de nuevo. El historial de esta conversación de Copilot tampoco se guarda
en los archivos del proyecto ni se recupera mediante `git clone`; el JSON conserva
el progreso de la auditoría, no la conversación.

### 2. Subir desde la máquina actual

El proyecto ya tiene Git inicializado. En una terminal, desde esta carpeta:

```bash
git status
git diff --cached --stat
```

Revisa el contenido y la lista de archivos preparados, especialmente los informes.
Si hay archivos que no debes subir, retíralos del área de preparación antes del
commit. Añade el README y los demás archivos autorizados:

```bash
git add .gitignore README.md audit.py crawler.py graph_store.py test_audit.py \
  requirements.txt campos.json grabacion_campos.json instrucciones_seo.json \
  resultado.json auditoria.json
git status
git diff --cached --stat
git commit -m "Guardar proyecto y progreso de auditoría"
```

Los archivos que ya estaban preparados también entrarán en el commit; comprueba
`git status` antes de crearlo. Si quieres incluir los HTML y aún no están
preparados, añádelos explícitamente con `git add resultado.html auditoria.html`.

Si todavía no tienes un repositorio remoto, autentícate con GitHub CLI y crea uno
privado (cambia `geo-seo` por el nombre deseado):

```bash
gh auth login
gh repo create geo-seo --private --source=. --remote=origin --push
```

Si ya configuraste `origin`, en vez de crear otro repositorio:

```bash
git remote -v
git push -u origin main
```

Si tu rama no se llama `main`, usa el nombre que muestra `git status -b`.
No ejecutes `git push` hasta revisar que el commit contiene solo archivos
autorizados y ningún dato de sesión.

### 3. Descargar y preparar la otra máquina

Instala Git, Python 3 con soporte para `venv`, GitHub CLI y
[Ollama](https://ollama.com/download). Estos comandos son para Linux con acceso a
un entorno gráfico, necesario para abrir Chromium y entrar a Drupal. La máquina
actual utiliza Python 3.14.4; no hay un rango de versiones fijado en el proyecto.

```bash
gh auth login
gh repo clone TU_USUARIO/geo-seo
cd geo-seo
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Sustituye `TU_USUARIO/geo-seo` por el propietario y nombre reales del repositorio.
Si Playwright informa que faltan bibliotecas del sistema, instala las dependencias
de Chromium siguiendo su mensaje; en una distribución Linux compatible puedes
utilizar `python -m playwright install-deps chromium` (puede requerir permisos
de administrador).

Inicia Ollama según las instrucciones de instalación de tu sistema y descarga
el modelo configurado actualmente:

```bash
ollama pull llama3.2:3b
curl -f http://127.0.0.1:11434/api/tags
```

Si el servicio no está iniciado, ejecuta `ollama serve` en otra terminal y vuelve
a comprobarlo. El modelo, endpoint y tiempo de espera están en `campos.json`:
si los cambias, descarga y usa el modelo correspondiente.

Para comprobar la instalación:

```bash
python -m unittest -v test_audit
```

### 4. Iniciar sesión y continuar

El perfil anterior no se descarga de GitHub. Para iniciar sesión en la nueva
máquina y volver a grabar los selectores, ejecuta:

```bash
python audit.py record --site https://www.eafit.edu.co
```

En el navegador abierto, inicia sesión con tu cuenta autorizada, abre un nodo en
edición y enfoca **todos** los campos que quieras auditar. Pulsa Enter en la
terminal al terminar. Este comando reemplaza `grabacion_campos.json` con los
campos seleccionados: no omitas los que necesites. Si no seleccionas ningún
campo, mostrará un error y conservará la configuración existente. La sesión
queda en el perfil local `drupal_state/`, que no debe subirse.

Para continuar usando el progreso descargado:

```bash
python audit.py run --chunk 10 --out auditoria.json
```

El comando es un **dry-run**: genera propuestas, pero no guarda cambios en
Drupal. Usa el `auditoria.json` existente como punto de partida; `resultado.json`
solo se usa como fuente si el archivo de salida no existe. Los nodos sin estado
y los que tienen estado `dry_run...` son elegibles, así que repetir el comando
puede volver a procesar los mismos nodos de prueba. Los estados de error no se
reintentan automáticamente. `--redo` vuelve a procesar nodos ya procesados, pero
no sobrescribe campos de Drupal que ya tengan contenido.

Para revisar el informe y regenerar su visualización:

```bash
python audit.py report --out auditoria.json --html auditoria.html
```

Solo después de revisar las propuestas y confirmar que quieres guardar cambios:

```bash
python audit.py run --apply --chunk 10 --out auditoria.json
```

`--apply` vuelve a leer Drupal y generar propuestas; no aplica literalmente las
propuestas guardadas en el dry-run, por lo que los resultados pueden diferir.
Revisa los mensajes, errores y páginas en el navegador. El estado `revisado` por
sí solo no confirma que todas las páginas o campos se hayan guardado correctamente.

Para transferir avances posteriores, revisa los cambios y la información incluida,
haz commit y push de `auditoria.json` en la máquina que los generó y, en la otra,
ejecuta `git pull` estando en la rama correspondiente. Evita trabajar
simultáneamente sobre el mismo archivo en ambas máquinas: Git podría producir
conflictos y no fusiona automáticamente el progreso de la auditoría.
