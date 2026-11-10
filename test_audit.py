import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import audit
from graph_store import ROOT


class AuditWorkflowTests(unittest.TestCase):
    def setUp(self):
        # La reescritura llama a Ollama directamente; en las pruebas no debe tocar el servidor real.
        parche = patch("audit.reescribir_longitud", return_value=None)
        parche.start()
        self.addCleanup(parche.stop)

    def test_recorded_fields_keep_selectors_without_values(self):
        events = [
            {
                "evento": "focusin",
                "type": "text",
                "selector": "#page-title",
                "name": "field_meta_tags[0][basic][title]",
                "label": "Page Title (Título de la página)",
                "seccion": "Basic Tags",
            },
            {
                "evento": "focusin",
                "type": "checkbox",
                "selector": "#robots-index",
                "name": "field_meta_tags[0][advanced][robots][robots][index]",
                "label": "index",
                "seccion": "Avanzado",
            },
        ]

        fields = audit.construir_campos_grabados(events)

        self.assertIn("page_title", fields)
        self.assertTrue(fields["page_title"]["contexto"])
        self.assertFalse(fields["robots_index"]["contexto"])
        self.assertTrue(fields["robots_index"]["default"])
        self.assertNotIn("valor", fields["page_title"])
        self.assertNotIn("el.value", audit.RECORD_JS)
        self.assertIn("password", audit.RECORD_JS)
        self.assertIn("el.checked = true", audit.RECORD_JS)
        self.assertIn("addEventListener('input'", audit.RECORD_JS)
        self.assertIn("addEventListener('change'", audit.RECORD_JS)

    def test_all_selected_filled_editorial_fields_feed_model_context(self):
        fields = {
            "page_title": {"label": "Page Title", "contexto": True},
            "description": {"label": "Description", "contexto": True},
            "canonical_url": {"contexto": False},
            "robots_index": {"tipo": "checkbox"},
            "abstract": {"label": "Abstract", "contexto": True},
        }
        values = {
            "page_title": "Centro de Valor Público",
            "description": "Investigación y consultoría",
            "canonical_url": "[current-page:url:absolute]",
            "robots_index": True,
            "abstract": "",
        }

        context = audit.construir_contexto_campos(fields, values, fields)

        self.assertEqual(set(context), {"page_title", "description"})
        self.assertEqual(context["description"]["valor"], "Investigación y consultoría")

    def test_field_rules_are_loaded_as_an_instruction_map(self):
        instructions = audit.cargar_instrucciones()
        fields = {rule["campo"]: rule for rule in instructions["campos"]}

        self.assertEqual((fields["page_title"]["min_caracteres"], fields["page_title"]["max_caracteres"]), (50, 60))
        self.assertEqual(fields["og_title"]["origen"], "page_title")
        self.assertEqual(fields["og_description"]["origen"], "description")
        self.assertTrue(instructions["checklist"])

    def test_run_requires_user_recorded_json_selectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fields.json"
            path.write_text(json.dumps({"campos": {"page_title": {"selector": "#title"}}}), encoding="utf-8")
            self.assertEqual(audit.cargar_campos_grabados(path), {"page_title": {"selector": "#title"}})

            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaisesRegex(audit.AuditProcessingError, "no contiene campos grabados"):
                audit.cargar_campos_grabados(path)

    def test_generated_values_obey_pdf_limits_and_content_rules(self):
        self.assertEqual(len(audit.validar_valor_generado(
            {"campo": "page_title", "min_caracteres": 50, "max_caracteres": 60},
            "a" * 50,
        )), 50)
        self.assertEqual(len(audit.validar_valor_generado(
            {"campo": "description", "min_caracteres": 150, "max_caracteres": 160},
            "a" * 150,
        )), 150)
        self.assertEqual(audit.validar_valor_generado(
            {"campo": "keywords", "max_caracteres": 255},
            "universidad, investigación",
        ), "universidad, investigación")

        with self.assertRaisesRegex(ValueError, "mínimo"):
            audit.validar_valor_generado(
                {"campo": "page_title", "min_caracteres": 50, "max_caracteres": 60},
                "a" * 49,
            )
        with self.assertRaisesRegex(ValueError, "siglas"):
            audit.validar_valor_generado({"campo": "abstract"}, "EAFIT PEI")
        with self.assertRaisesRegex(ValueError, "marcador de ausencia"):
            audit.validar_valor_generado({"campo": "abstract"}, "null")
        with self.assertRaisesRegex(ValueError, "menos de 8 palabras"):
            audit.validar_valor_generado(
                {"campo": "abstract", "min_palabras": 8, "max_caracteres": 159},
                "Centro EAFIT en Medellín, Colombia.",
            )
        with self.assertRaisesRegex(ValueError, "separados por comas"):
            audit.validar_valor_generado({"campo": "keywords"}, "investigación")

    def test_slightly_overlong_description_is_trimmed_at_word_boundary(self):
        instruction = {"campo": "description", "min_caracteres": 150, "max_caracteres": 160}
        text = (
            "Universidad EAFIT en Medellín, centro de investigación aplicada, consultoría y análisis "
            "de políticas públicas para instituciones y comunidades en Medellín, Colombia."
        )

        value = audit.ajustar_sobrelongitud(instruction, text)

        self.assertLessEqual(len(value), 160)
        self.assertGreaterEqual(len(value), 150)
        self.assertTrue(value.endswith("."))
        self.assertEqual(audit.validar_valor_generado(instruction, value), value)

    def test_ollama_requires_a_local_endpoint_and_structured_output(self):
        response = Mock()
        response.json.return_value = {"response": '{"page_title":"Centro de valor para investigación | Universidad EAFIT"}'}
        rules = {"page_title": {"instruccion": "Genera un título", "min_caracteres": 10, "max_caracteres": 80}}
        config = {"endpoint": "http://127.0.0.1:11434/api/generate", "nombre": "llama3.2:3b", "timeout": 180}

        with patch("audit.requests.post", return_value=response) as post:
            result = audit.generar_con_ollama(["page_title"], rules, {"h1": "Centro"}, config)

        self.assertEqual(result["page_title"], "Centro de valor para investigación | Universidad EAFIT")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "llama3.2:3b")
        self.assertEqual(post.call_args.kwargs["json"]["format"]["required"], ["page_title"])

        with self.assertRaisesRegex(audit.AuditProcessingError, "localhost"):
            audit.generar_con_ollama(
                ["page_title"],
                rules,
                {},
                {"endpoint": "https://example.com/api/generate"},
            )

    def test_invalid_model_output_gets_one_constraint_repair(self):
        instruction = {"description": {"campo": "description", "min_caracteres": 2, "max_caracteres": 5}}
        with patch(
            "audit.generar_con_ollama",
            side_effect=[{"description": "demasiado largo"}, {"description": "bien"}],
        ) as generate:
            values, errors = audit.generar_campos_validados(
                ["description"], instruction, {"pagina": {}}, audit.OLLAMA_DEFAULT,
            )

        self.assertEqual(values, {"description": "bien"})
        self.assertEqual(errors, {})
        self.assertEqual(generate.call_count, 2)
        self.assertIn("correcciones_solicitadas", generate.call_args_list[1].args[2])

    def test_null_model_output_is_sent_for_human_review_without_retry(self):
        instruction = {"description": {"campo": "description", "min_caracteres": 2, "max_caracteres": 5}}
        with patch("audit.generar_con_ollama", return_value={"description": None}) as generate:
            values, errors = audit.generar_campos_validados(
                ["description"], instruction, {"pagina": {}}, audit.OLLAMA_DEFAULT,
            )

        self.assertEqual(values, {})
        self.assertIn("información factual", errors["description"])
        generate.assert_called_once()

    def test_abstract_matching_existing_description_is_retried(self):
        rules = {"abstract": {"campo": "abstract", "min_palabras": 8, "max_caracteres": 159}}
        description = "La universidad ofrece investigación y servicios para las organizaciones de Medellín."
        distinct = "Centro universitario de investigación aplicada para organizaciones en Medellín, Colombia."
        context = {"campos_seleccionados_llenos": {"description": {"valor": description}}}
        with patch("audit.generar_con_ollama", side_effect=[
            {"abstract": description}, {"abstract": distinct},
        ]) as generate:
            values, errors = audit.generar_campos_validados(
                ["abstract"], rules, context, audit.OLLAMA_DEFAULT,
            )

        self.assertEqual(values, {"abstract": distinct})
        self.assertEqual(errors, {})
        self.assertIn("description", generate.call_args_list[1].args[2]["correcciones_solicitadas"]["abstract"]["motivo"])

    def test_duplicate_generated_fields_are_not_accepted_after_retries(self):
        rules = {
            "description": {"campo": "description", "max_caracteres": 160},
            "abstract": {"campo": "abstract", "max_caracteres": 159},
        }
        text = "Universidad EAFIT ofrece investigación aplicada para organizaciones en Medellín."
        with patch("audit.generar_con_ollama", side_effect=[
            {"description": text, "abstract": text},
            {"abstract": text},
            {"abstract": text},
            {"abstract": text},
            {"abstract": text},
        ]):
            values, errors = audit.generar_campos_validados(
                ["description", "abstract"], rules, {"pagina": {}}, {**audit.OLLAMA_DEFAULT, "por_campo": False},
            )

        self.assertEqual(values, {"description": text})
        self.assertIn("description", errors["abstract"])

    def test_abstract_length_limit_and_failed_repairs(self):
        rule = {"campo": "abstract", "min_palabras": 8, "max_caracteres": 159}
        exact = "palabra " * 7 + "a" * 103
        self.assertEqual(len(exact), 159)
        self.assertEqual(audit.validar_valor_generado(rule, exact), exact)
        with self.assertRaisesRegex(ValueError, "160 caracteres; el máximo es 159"):
            audit.validar_valor_generado(rule, exact + "a")

        long_text = "palabra " * 7 + "a" * 235
        self.assertEqual(len(long_text), 291)
        with patch("audit.generar_con_ollama", return_value={"abstract": long_text}) as generate:
            values, errors = audit.generar_campos_validados(
                ["abstract"], {"abstract": rule}, {}, audit.OLLAMA_DEFAULT,
            )
        self.assertEqual(values, {})
        self.assertIn("291 caracteres; el máximo es 159", errors["abstract"])
        self.assertEqual(generate.call_count, 5)

    def test_generated_description_must_differ_from_existing_abstract(self):
        text = "Centro universitario de investigación aplicada para organizaciones en Medellín."
        distinct = "La universidad desarrolla investigación para organizaciones de Medellín."
        rules = {"description": {"campo": "description", "max_caracteres": 160}}
        context = {"campos_seleccionados_llenos": {"abstract": {"valor": text}}}
        with patch("audit.generar_con_ollama", side_effect=[
            {"description": text}, {"description": distinct},
        ]):
            values, errors = audit.generar_campos_validados(
                ["description"], rules, context, audit.OLLAMA_DEFAULT,
            )
        self.assertEqual(values, {"description": distinct})
        self.assertEqual(errors, {})

    def test_duplicate_is_detected_after_description_repair(self):
        text = "Centro universitario de investigación aplicada para organizaciones en Medellín."
        distinct = "Investigación universitaria para organizaciones e instituciones de Medellín."
        rules = {
            "description": {"campo": "description", "max_caracteres": 160},
            "abstract": {"campo": "abstract", "max_caracteres": 159},
        }
        with patch("audit.generar_con_ollama", side_effect=[
            {"description": "x" * 200, "abstract": text},
            {"description": text},
            {"abstract": distinct},
        ]):
            values, errors = audit.generar_campos_validados(
                ["description", "abstract"], rules, {}, audit.OLLAMA_DEFAULT,
            )
        self.assertEqual(values, {"description": text, "abstract": distinct})
        self.assertEqual(errors, {})

    def test_non_string_model_value_is_rejected_without_crashing(self):
        rule = {"campo": "abstract", "max_caracteres": 159}
        with patch("audit.generar_con_ollama", return_value={"abstract": 291}):
            values, errors = audit.generar_campos_validados(
                ["abstract"], {"abstract": rule}, {}, audit.OLLAMA_DEFAULT,
            )
        self.assertEqual(values, {})
        self.assertIn("no textual", errors["abstract"])

    def test_generated_abstract_is_applied_to_its_own_selector(self):
        description = "La universidad ofrece investigación y servicios para las organizaciones de Medellín."
        abstract = "Centro universitario de investigación aplicada para organizaciones en Medellín, Colombia."
        fields = {
            "description": {"selector": "#description", "contexto": True},
            "abstract": {"selector": "#abstract", "contexto": True},
        }
        rules = [
            {"campo": "description", "modo": "modelo", "max_caracteres": 160},
            {"campo": "abstract", "modo": "modelo", "min_palabras": 8, "max_caracteres": 159},
        ]
        page = Mock()
        page.url = "https://example.test/node/1/edit"
        elements = {name: Mock() for name in fields}
        ctx = Mock()
        ctx.new_page.return_value = page
        content = {"h1_count": 1, "h1": "Centro", "primer_parrafo": "Texto", "imagenes_sin_alt": 0}
        with patch("audit.extraer_contexto_pagina", return_value=content), \
             patch("audit.enlace_editar", return_value="/node/1/edit"), \
             patch("audit.leer", side_effect=lambda page, field: (
                 elements[field["selector"][1:]],
                 description if field["selector"] == "#description" else "",
             )), \
             patch("audit.generar_con_ollama", return_value={"abstract": abstract}):
            info, _ = audit.procesar(
                ctx, "https://example.test/centro", fields, rules, [], audit.OLLAMA_DEFAULT, True, "#save",
            )
        elements["abstract"].fill.assert_called_once_with(abstract)
        elements["description"].fill.assert_not_called()
        self.assertEqual(info["campos"]["abstract"]["accion"], "llenado_sin_guardar")
        self.assertTrue(info["pendiente_guardar"])
        page.locator.assert_not_called()  # no pulsa Guardar: eso ocurre con el primer Enter

    def test_existing_abstract_is_preserved_and_flagged_for_review(self):
        text = "Texto existente " * 20
        fields = {
            "description": {"selector": "#description"},
            "abstract": {"selector": "#abstract"},
        }
        rules = [
            {"campo": "description", "modo": "modelo", "max_caracteres": 160},
            {"campo": "abstract", "modo": "modelo", "max_caracteres": 159},
        ]
        page = Mock()
        page.url = "https://example.test/node/1/edit"
        elements = {name: Mock() for name in fields}
        ctx = Mock()
        ctx.new_page.return_value = page
        content = {"h1_count": 1, "h1": "Centro", "primer_parrafo": "Texto", "imagenes_sin_alt": 0}
        with patch("audit.extraer_contexto_pagina", return_value=content), \
             patch("audit.enlace_editar", return_value="/node/1/edit"), \
             patch("audit.leer", side_effect=lambda page, field: (elements[field["selector"][1:]], text)):
            info, _ = audit.procesar(
                ctx, "https://example.test/centro", fields, rules, [], audit.OLLAMA_DEFAULT, True, "#save",
            )

        self.assertEqual(info["campos"]["abstract"]["accion"], "sin_cambios")
        self.assertEqual(info["campos"]["abstract"]["nuevo"], text)
        self.assertIn("excede_limite_abstract", info["marcas"])
        self.assertIn("abstract_igual_description", info["marcas"])
        elements["abstract"].fill.assert_not_called()
        page.locator.assert_not_called()

    def test_instructions_refuse_unavailable_facts_and_protect_manual_fields(self):
        instructions = {rule["campo"]: rule for rule in audit.cargar_instrucciones()["campos"]}

        self.assertEqual(instructions["page_title"]["modo"], "modelo")
        self.assertEqual(instructions["canonical_url"]["modo"], "manual")
        self.assertEqual(instructions["og_title"]["modo"], "copiar")
        self.assertIn("Fórmula", instructions["abstract"]["instruccion"])
        self.assertIn("palabras clave", instructions["abstract"]["instruccion"])
        self.assertNotIn("schema_name", instructions)

    def test_abstract_that_lists_keywords_is_detected(self):
        keywords = "Universidad EAFIT, información institucional EAFIT, PEI EAFIT, historia de EAFIT, estatutos EAFIT"
        lista = "La Universidad EAFIT ofrece información institucional EAFIT, PEI EAFIT e historia de EAFIT en Medellín."
        sintesis = "Desarrollamos y compartimos el marco institucional de la Universidad EAFIT en Medellín, Colombia."
        self.assertTrue(audit.parece_lista_keywords(lista, keywords))
        self.assertFalse(audit.parece_lista_keywords(sintesis, keywords))

    def test_console_summary_shows_filled_fields_with_content(self):
        nodo = {
            "estado": "dry_run_automatico_con_marcas",
            "marcas": ["imagenes_sin_alt"],
            "revision_contenido": {"imagenes_sin_alt": 2},
            "campos": {
                "abstract": {"previo": "", "nuevo": "Texto nuevo", "accion": "pendiente_dry_run",
                             "origen": "modelo", "modelo": "gemma3:4b"},
                "og_locale": {"previo": "", "nuevo": "es_CO", "accion": "pendiente_dry_run", "origen": "predeterminado"},
                "page_title": {"previo": "", "nuevo": None, "accion": "error_modelo", "error": "tiene 44 caracteres"},
                "keywords": {"previo": "a, b", "nuevo": "a, b", "accion": "sin_cambios"},
            },
        }
        texto = audit.resumen_pagina("https://x", nodo, "gemma3:4b", apply=False)
        self.assertIn("SE LLENARÍA (2)", texto)
        self.assertIn('+ abstract  [IA gemma3:4b]', texto)
        self.assertIn('nuevo: "Texto nuevo" (11 car.)', texto)
        self.assertIn("! page_title: tiene 44 caracteres", texto)
        self.assertIn("SIN CAMBIOS: keywords", texto)
        self.assertIn("2 imágenes sin alt", texto)

    def test_reset_removes_only_processing_states(self):
        nodes = {
            ROOT: {"title": "root", "estado": "root-state"},
            "https://example.test/a": {"estado": "revisado", "marcas": ["nota"]},
            "https://example.test/b": {"title": "B"},
        }

        self.assertEqual(audit.reiniciar_estados(nodes), 1)
        self.assertIn("estado", nodes[ROOT])
        self.assertNotIn("estado", nodes["https://example.test/a"])
        self.assertEqual(nodes["https://example.test/a"]["marcas"], ["nota"])
        self.assertEqual(nodes["https://example.test/b"]["title"], "B")

    def test_ai_proposals_are_identifiable_for_terminal_and_report_evidence(self):
        node = {
            "modelo_ia": "llama3.2:3b",
            "campos": {
                "page_title": {
                    "nuevo": "Centro de Investigación | Universidad EAFIT",
                    "accion": "pendiente_dry_run",
                    "origen": "modelo",
                    "modelo": "llama3.2:3b",
                },
                "og_title": {"nuevo": "Centro de Investigación", "origen": "page_title"},
                "keywords": {
                    "nuevo": None,
                    "accion": "error_modelo",
                    "origen": "modelo",
                    "error": "Falta información",
                },
            },
        }

        proposals = audit.campos_propuestos_por_ia(node)

        self.assertEqual(set(proposals), {"page_title", "keywords"})
        self.assertEqual(proposals["page_title"]["modelo"], "llama3.2:3b")
        self.assertEqual(proposals["keywords"]["accion"], "error_modelo")

    def test_page_title_suffix_is_swapped_to_fit_range(self):
        rule = {"campo": "page_title", "min_caracteres": 50, "max_caracteres": 60}
        largo = "Centro de Valor Público: Investigación y Consultoría | Universidad EAFIT"
        self.assertEqual(
            audit.ajustar_sobrelongitud(rule, largo),
            "Centro de Valor Público: Investigación y Consultoría | EAFIT",
        )
        corto = "Universidad EAFIT: Información Institucional | EAFIT"
        corto_sin_rango = "Información institucional | EAFIT"
        self.assertEqual(audit.ajustar_sobrelongitud(rule, corto), corto)
        self.assertEqual(audit.ajustar_sobrelongitud(rule, corto_sin_rango), corto_sin_rango)

    def test_long_abstract_is_cut_at_clause_boundary(self):
        rule = {"campo": "abstract", "max_caracteres": 159}
        texto = ("La Universidad EAFIT es una institución de conocimiento aplicado en Medellín, Colombia, "
                 "con investigación en ingeniería y ciencias, además de consultoría para empresas del sector público.")
        ajustado = audit.ajustar_sobrelongitud(rule, texto)
        self.assertLessEqual(len(ajustado), 159)
        self.assertTrue(ajustado.endswith("."))
        self.assertTrue(texto.startswith(ajustado[:-1]))

    def test_existing_invalid_values_are_detected_for_correction(self):
        reglas = {r["campo"]: r for r in audit.cargar_instrucciones()["campos"]}
        keywords = "Universidad EAFIT, información institucional EAFIT, PEI EAFIT, historia de EAFIT, estatutos EAFIT"
        valores = {"keywords": keywords, "description": "Otra cosa"}
        self.assertIn("igual a keywords", audit.motivo_valor_existente_invalido(reglas["abstract"], keywords, valores))
        self.assertIn("mínimo es 50", audit.motivo_valor_existente_invalido(
            reglas["page_title"], "Información institucional", valores))
        self.assertIsNone(audit.motivo_valor_existente_invalido(reglas["abstract"], "", valores))

    def test_retry_instruction_says_how_many_words_to_cut(self):
        rule = {"campo": "abstract", "max_caracteres": 159}
        self.assertIn("Elimina unas", audit.instruccion_ajuste(rule, "a" * 200))
        self.assertIn("Agrega unas", audit.instruccion_ajuste({"min_caracteres": 50}, "a" * 30))
        self.assertEqual(audit.palabras_objetivo(rule), "máximo 22 palabras")


if __name__ == "__main__":
    unittest.main()
