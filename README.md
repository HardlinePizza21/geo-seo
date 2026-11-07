# Crear la sesión de Drupal

En la carpeta del proyecto, con las dependencias y Chromium de Playwright
instalados y acceso a un entorno gráfico, ejecuta:

```bash
python audit.py record --site https://www.eafit.edu.co
```

En el navegador que se abre, inicia sesión en Drupal, entra a un nodo en edición
y enfoca todos los campos que quieras auditar. Después pulsa Enter en la terminal.
El comando crea `drupal_state/` con el perfil local del navegador y reemplaza
`grabacion_campos.json` con los campos seleccionados. Si no seleccionas ningún
campo, conserva la configuración anterior.


# 1. Tú controlas la grabación de los campos; guarda los selectores en JSON
./.venv/bin/python audit.py record --site https://www.eafit.edu.co

# 2. Simular el primer bloque de 10 páginas, sin guardar en Drupal
./.venv/bin/python audit.py run --source resultado.json --out auditoria.json --chunk 10

# 3. Generar/revisar el informe visual
./.venv/bin/python audit.py report --out auditoria.json --html auditoria.html

# 4. Cuando revises el bloque, aplicarlo en Drupal
./.venv/bin/python audit.py run --apply --chunk 10 --out auditoria.json
