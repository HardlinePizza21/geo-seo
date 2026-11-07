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

No subas `drupal_state/` a GitHub: puede contener cookies y credenciales de sesión.
