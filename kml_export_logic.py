# -*- coding: utf-8 -*-
"""
Lógica de exportación a KML usando simplekml.
Reemplaza el enfoque anterior basado en QgsVectorFileWriter para tener
control total sobre SchemaData, BalloonStyle y simbología.
"""

import os
import sys

_LIBS_PATH = os.path.join(os.path.dirname(__file__), "libs")
if _LIBS_PATH not in sys.path:
    sys.path.insert(0, _LIBS_PATH)

import tempfile
from qgis.PyQt.QtCore import QSize, QByteArray, QBuffer, QIODevice

import simplekml
import os
from qgis.core import (
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsWkbTypes,
    QgsRenderContext,
    QgsFeatureRequest,
    NULL,
)


def _qgis_field_type_to_kml(field_type_name):
    """Traduce el tipo de campo de QGIS al tipo esperado por el Schema de KML."""
    mapping = {
        "int": "int",
        "int2": "int",
        "int4": "int",
        "int8": "int",
        "integer": "int",
        "double": "double",
        "double precision": "double",
        "real": "float",
        "numeric": "double",
        "string": "string",
        "text": "string",
        "varchar": "string",
        "date": "string",
        "datetime": "string",
        "bool": "bool",
    }
    return mapping.get(field_type_name.lower(), "string")


def _build_balloon_text(schema_name, included_field_names, has_name, has_description):
    parts = []
    if has_name:
        parts.append("<h3>$[name]</h3>")
    if has_description:
        parts.append("<p>$[description]</p>")

    if included_field_names:  # <- solo agrega tabla si hay campos
        rows = "".join(
            f"<tr><td><b>{field}</b></td><td>$[{schema_name}/{field}]</td></tr>"
            for field in included_field_names
        )
        parts.append(f'<table border="1" cellpadding="4">{rows}</table>')

    return "".join(parts)


def _geometry_to_kml(kml_folder, placemark_name, qgs_geometry):
    """
    Crea la geometría del placemark dentro de simplekml a partir de una
    QgsGeometry ya reproyectada a WGS84. Soporta Point, LineString, Polygon
    y sus variantes Multi. Devuelve el objeto (Point/LineString/Polygon/
    MultiGeometry) que ya expone .name, .description, .style, .extendeddata.
    """
    geom_type = qgs_geometry.type()  # 0=Point, 1=Line, 2=Polygon

    if geom_type == 0:  # Punto
        if qgs_geometry.isMultipart():
            placemark = kml_folder.newmultigeometry(name=placemark_name)
            for pt in qgs_geometry.asMultiPoint():
                placemark.newpoint(coords=[(pt.x(), pt.y())])
        else:
            pt = qgs_geometry.asPoint()
            placemark = kml_folder.newpoint(
                name=placemark_name, coords=[(pt.x(), pt.y())]
            )

    elif geom_type == 1:  # Línea
        if qgs_geometry.isMultipart():
            placemark = kml_folder.newmultigeometry(name=placemark_name)
            for line in qgs_geometry.asMultiPolyline():
                coords = [(pt.x(), pt.y()) for pt in line]
                placemark.newlinestring(coords=coords)
        else:
            line = qgs_geometry.asPolyline()
            coords = [(pt.x(), pt.y()) for pt in line]
            placemark = kml_folder.newlinestring(name=placemark_name, coords=coords)

    elif geom_type == 2:  # Polígono
        if qgs_geometry.isMultipart():
            placemark = kml_folder.newmultigeometry(name=placemark_name)
            for polygon in qgs_geometry.asMultiPolygon():
                outer = [(pt.x(), pt.y()) for pt in polygon[0]]
                pol = placemark.newpolygon(outerboundaryis=outer)
                if len(polygon) > 1:
                    pol.innerboundaryis = [
                        [(pt.x(), pt.y()) for pt in ring] for ring in polygon[1:]
                    ]
        else:
            polygon = qgs_geometry.asPolygon()
            outer = [(pt.x(), pt.y()) for pt in polygon[0]]
            placemark = kml_folder.newpolygon(name=placemark_name, outerboundaryis=outer)
            if len(polygon) > 1:
                placemark.innerboundaryis = [
                    [(pt.x(), pt.y()) for pt in ring] for ring in polygon[1:]
                ]

    else:
        raise ValueError(f"Tipo de geometría no soportado: {geom_type}")

    return placemark


def _qcolor_to_kml(qcolor):
    """Convierte un QColor de QGIS al formato de color KML (AABBGGRR)."""
    return simplekml.Color.rgb(qcolor.red(), qcolor.green(), qcolor.blue(), qcolor.alpha())


_GOOGLE_ICON_BASE = "http://maps.google.com/mapfiles/kml/shapes/"
_GOOGLE_ICON_BY_SHAPE = {
    "circle": "placemark_circle.png",
    "square": "square.png",
    "rectangle": "square.png",
    "diamond": "diamond.png",
    "triangle": "triangle.png",
    "star": "star.png",
    "cross": "cross-hairs.png",
    "pentagon": "star.png",
}


def _detect_google_icon_url(symbol):
    """Intenta mapear la forma del símbolo de QGIS a un ícono clásico de
    Google Earth. Si no se reconoce la forma, usa el círculo por defecto."""
    for layer in symbol.symbolLayers():
        if hasattr(layer, "shape"):
            try:
                shape_enum = layer.shape()
                shape_name = shape_enum.name.lower() if hasattr(shape_enum, "name") else str(shape_enum).lower()
            except Exception:
                continue
            for key, filename in _GOOGLE_ICON_BY_SHAPE.items():
                if key in shape_name:
                    return _GOOGLE_ICON_BASE + filename
    return _GOOGLE_ICON_BASE + "placemark_circle.png"  # fallback


def _symbol_to_temp_png(symbol, size=64):
    """Renderiza el símbolo real de QGIS a un PNG temporal en disco.
    Devuelve la ruta del archivo temporal (el llamador debe borrarlo después)."""
    image = symbol.asImage(QSize(size, size))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    image.save(tmp.name, "PNG")
    return tmp.name


def _extract_symbol_style(symbol, geom_type, layer_opacity=1.0):
    """
    Extrae color de relleno, borde e ícono de un QgsSymbol, incorporando
    tanto la opacidad del símbolo como la opacidad global de la capa
    (dos multiplicadores independientes del alfa del color en QGIS).
    """
    props = {}
    symbol_opacity = symbol.opacity() if hasattr(symbol, "opacity") else 1.0
    total_opacity = max(0.0, min(1.0, symbol_opacity * layer_opacity))

    def _color_with_opacity(qcolor):
        alpha = int(round(qcolor.alpha() * total_opacity))
        return simplekml.Color.rgb(qcolor.red(), qcolor.green(), qcolor.blue(), alpha)

    if geom_type == 2:  # Polígono
        fill_color = symbol.color()
        effective_fill_alpha = fill_color.alpha() * total_opacity
        props["has_fill"] = effective_fill_alpha > 0
        props["fill_color"] = _color_with_opacity(fill_color)

        stroke_color = None
        stroke_width = 1.0
        for slayer in symbol.symbolLayers():
            if hasattr(slayer, "strokeColor"):
                stroke_color = slayer.strokeColor()
                stroke_width = slayer.strokeWidth() if hasattr(slayer, "strokeWidth") else 1.0
                break
            elif hasattr(slayer, "color") and hasattr(slayer, "width"):
                stroke_color = slayer.color()
                stroke_width = slayer.width()
                break

        if stroke_color is not None:
            effective_line_alpha = stroke_color.alpha() * total_opacity
            props["has_line"] = effective_line_alpha > 0
            props["line_color"] = _color_with_opacity(stroke_color)
            props["line_width"] = max(stroke_width * 3.78, 0.5)
        else:
            props["has_line"] = False

    elif geom_type == 1:  # Línea
        line_color = symbol.color()
        width = 1.0
        for slayer in symbol.symbolLayers():
            if hasattr(slayer, "width"):
                width = slayer.width()
                break
        props["line_color"] = _color_with_opacity(line_color)
        props["line_width"] = max(width * 3.78, 0.5)

    elif geom_type == 0:  # Punto
        point_color = symbol.color()
        props["icon_color"] = _color_with_opacity(point_color)
        size = symbol.size() if hasattr(symbol, "size") else 3.0
        props["icon_scale"] = max(size / 3.0, 0.5)

    return props


def _apply_style_to_placemark(placemark, props, geom_type, symbol=None,
                               usar_iconos_clasicos=True, kml=None, icon_cache=None):
    if geom_type == 2:  # Polígono
        placemark.style.polystyle.fill = 1 if props.get("has_fill") else 0
        if props.get("has_fill"):
            placemark.style.polystyle.color = props["fill_color"]
        placemark.style.polystyle.outline = 1 if props.get("has_line") else 0
        if props.get("has_line"):
            placemark.style.linestyle.color = props["line_color"]
            placemark.style.linestyle.width = props["line_width"]

    elif geom_type == 1:  # Línea
        placemark.style.linestyle.color = props["line_color"]
        placemark.style.linestyle.width = props["line_width"]

    elif geom_type == 0:  # Punto
        if usar_iconos_clasicos:
            placemark.style.iconstyle.color = props["icon_color"]
            placemark.style.iconstyle.scale = props["icon_scale"]
            placemark.style.iconstyle.icon.href = _detect_google_icon_url(symbol)
        else:
            cache_key = id(symbol)
            if cache_key not in icon_cache:
                tmp_path = _symbol_to_temp_png(symbol)
                kmz_path = kml.addfile(tmp_path)
                icon_cache[cache_key] = (kmz_path, tmp_path)
            kmz_path, _ = icon_cache[cache_key]
            placemark.style.iconstyle.scale = props["icon_scale"]
            placemark.style.iconstyle.icon.href = kmz_path


def export_layer_to_kml(layer, field_mapping, output_path, name_field=None,
                        description_field=None, document_name=None, folder_name=None,
                        feature_request=None, usar_iconos_clasicos=True,
                        group_field=None):
    """
    Exporta una capa vectorial de QGIS a KML usando simplekml, conservando:
    - SchemaData con los campos y nombres elegidos por el usuario (opcional)
    - BalloonStyle combinado (name + description + tabla), cada parte opcional
    - Estilos de la capa (símbolos)
    """
    if layer is None or not layer.isValid():
        raise ValueError("La capa no es válida.")
    if not field_mapping and name_field is None and description_field is None:
        raise ValueError("No hay nada que exportar (ni tabla, ni Name, ni Description).")
    if group_field and layer.fields().indexFromName(group_field) == -1:
        raise ValueError(f"El campo de agrupación '{group_field}' no existe en la capa.")

    # --- Reproyección a WGS84 (obligatorio para KML) ---
    dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())

    default_layer_name = layer.name()

    if not document_name:
        document_name = default_layer_name

    if not folder_name:
        folder_name = default_layer_name

    kml = simplekml.Kml(name=document_name)

    original_fields = layer.fields()
    included_names = list(field_mapping.keys())

    # --- Schema: se crea UNA sola vez, y solo si hay campos incluidos ---
    schema = None
    schema_name = ""
    if included_names:
        schema_name = document_name.replace(" ", "_")
        schema = kml.newschema(name=schema_name)
        for original_name in included_names:
            field = original_fields.field(original_name)
            kml_type = _qgis_field_type_to_kml(field.typeName())
            schema.newsimplefield(
                name=field_mapping[original_name],
                type=kml_type,
                displayname=field_mapping[original_name],
            )

    # Con agrupación no se crea carpeta contenedora: las carpetas por valor
    # van directamente bajo el documento.
    folder = None if group_field else kml.newfolder(name=folder_name)

    group_folders = {}

    balloon_text = _build_balloon_text(
        schema_name,
        [field_mapping[n] for n in included_names],
        has_name=name_field is not None,
        has_description=description_field is not None,
    )

    renderer = layer.renderer()
    render_context = QgsRenderContext()
    layer_opacity = layer.opacity() if hasattr(layer, "opacity") else 1.0
    if renderer is not None:
        renderer.startRender(render_context, layer.fields())

    icon_cache = {}

    for feature in layer.getFeatures(feature_request or QgsFeatureRequest()):
        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            continue
        geom.transform(transform)

        placemark_name = (
            str(feature.attribute(name_field)) if name_field else ""
        )
        placemark_desc = (
            str(feature.attribute(description_field)) if description_field else ""
        )

        # 1) Primero se crea el placemark (geometría)
        target_folder = folder
        if group_field:
            raw = feature.attribute(group_field)
            if raw is None or raw == NULL or str(raw).strip() == "":
                group_name = "Sin clasificar"
            else:
                group_name = str(raw).strip()
            if group_name not in group_folders:
                group_folders[group_name] = kml.newfolder(name=group_name)
            target_folder = group_folders[group_name]

        placemark = _geometry_to_kml(target_folder, placemark_name, geom)
        placemark.description = placemark_desc

        # 2) SchemaData: solo si hay campos incluidos, y ya con placemark creado
        if included_names:
            schemadata = simplekml.SchemaData(schemaurl=schema.id)
            for original_name in included_names:
                value = feature.attribute(original_name)
                schemadata.newsimpledata(
                    name=field_mapping[original_name],
                    value="" if value is None else str(value),
                )
            placemark.extendeddata.schemadata = schemadata

        # 3) BalloonStyle combinado (name + description + tabla)
        placemark.style.balloonstyle.text = balloon_text

        # 4) Estilo del símbolo
        if renderer is not None:
            symbol = renderer.symbolForFeature(feature, render_context)
            if symbol is not None:
                style_props = _extract_symbol_style(symbol, geom.type(), layer_opacity=layer_opacity)
                _apply_style_to_placemark(
                    placemark, style_props, geom.type(),
                    symbol=symbol, usar_iconos_clasicos=usar_iconos_clasicos,
                    kml=kml, icon_cache=icon_cache
                )

    # stopRender fuera del for, una sola vez al terminar
    if renderer is not None:
        renderer.stopRender(render_context)

    hay_imagenes_embebidas = bool(icon_cache)

    if hay_imagenes_embebidas:
        if not output_path.lower().endswith(".kmz"):
            output_path = os.path.splitext(output_path)[0] + ".kmz"
        kml.savekmz(output_path)
    else:
        kml.save(output_path)

    # Limpiar los PNG temporales, ya empaquetados dentro del kmz
    for _, tmp_path in icon_cache.values():
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return output_path

def _sanitize_filename(value):
    """Convierte un valor de campo en un nombre de archivo seguro para Windows/Linux/Mac."""
    text = str(value) if value is not None else "sin_valor"
    invalid_chars = '<>:"/\\|?*'
    for ch in invalid_chars:
        text = text.replace(ch, "_")
    text = text.strip().strip(".")
    return text or "sin_valor"


def export_layer_to_kml_batch(layer, field_mapping, output_folder, batch_field,
                               name_field=None, description_field=None,
                               usar_iconos_clasicos=True):
    """
    Genera un archivo .kml por cada valor único de batch_field.
    El nombre del archivo, el nombre del documento y el nombre de la
    carpeta interna del KML son siempre el mismo texto: el valor de ese campo.
    """
    if not batch_field:
        raise ValueError("No se seleccionó un campo para separar los archivos.")

    idx = layer.fields().indexFromName(batch_field)
    if idx == -1:
        raise ValueError(f"El campo '{batch_field}' no existe en la capa.")

    unique_values = layer.uniqueValues(idx)
    generated_files = []

    for value in sorted(unique_values, key=lambda v: str(v)):
        safe_name = _sanitize_filename(value)
        output_path = os.path.join(output_folder, f"{safe_name}.kml")

        if value is None:
            expression = f'"{batch_field}" IS NULL'
        elif isinstance(value, str):
            escaped = value.replace("'", "''")
            expression = f"\"{batch_field}\" = '{escaped}'"
        else:
            expression = f'"{batch_field}" = {value}'

        request = QgsFeatureRequest().setFilterExpression(expression)

        export_layer_to_kml(
            layer, field_mapping, output_path,
            name_field=name_field,
            description_field=description_field,
            document_name=safe_name,
            folder_name=safe_name,
            feature_request=request,
            usar_iconos_clasicos=usar_iconos_clasicos
        )
        generated_files.append(output_path)

    return generated_files