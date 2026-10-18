"""IFC (STEP-21) identity reader and normalizer."""

from __future__ import annotations

from pathlib import Path

from tizertmaint_pipeline.domain.evidence import BlockKind, LocatorKind
from tizertmaint_pipeline.domain.models import UnitRecord
from tizertmaint_pipeline.domain.routing import Route, UnitKind
from tizertmaint_pipeline.stages.normalizers.ifc import (
    decode_string,
    normalize_ifc,
    read_ifc,
    split_arguments,
)

HASH = "c" * 64

# Shaped like the corpus exports: identifiers on the project and site, geometry everywhere else.
MODEL = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition[CoordinationView_V2.0]'),'2;1');
FILE_NAME('SP0904-6032-M-MODL-20274_001-REV00.ifc','2024-03-01T07:53:05',(''),('TIZERT'),
'IFC Toolkit 2.0.26.0','Advance Steel 2024','');
FILE_SCHEMA(('IFC2X3'));
ENDSEC;
DATA;
#10=IFCPROJECT('g1',#150,'6032-ZA-1274','','','Goulotte d''alimentation','',(#20),#70);
#210=IFCSITE('3aLLBX_pL49eP2wkhJF75I',#150,'6032-ZA-1274',$,$,#220,$,$,.ELEMENT.,$,$,$,$,$);
#300=IFCCARTESIANPOINT((1250.,-6032.,1274.));
#310=IFCBEAM('0a',#150,'BEAM',$,'UPN200',#311,#312,'1043');
#320=IFCPLATE('0b',#150,'PLATE',$,$,#321,#322,'2201');
#330=IFCPLATE('0c',#150,'PLATE',$,$,#331,#332,
'2202');
#340=IFCELEMENTASSEMBLY('0d',#9,'Chute 6032-ZA-1110 liner',$,$,#3,#4,$,.N.,.N.);
#350=IFCMATERIAL('S355JR');
#351=IFCMATERIAL('S355JR');
#360=IFCPROPERTYSINGLEVALUE('Reference',$,IFCLABEL('SP0904-M023.00-G02-0013'),$);
#370=IFCPROPERTYSINGLEVALUE('Weight',$,IFCREAL(12.5),$);
#380=IFCBUILDINGSTOREY('0e',#9,'Niveau \\X2\\00E9\\X0\\l\\X\\E9vation',$,$,#3,$,'Plancher',.E.,0.);
ENDSEC;
END-ISO-10303-21;
"""


def _unit() -> UnitRecord:
    return UnitRecord(
        unit_id="unit:ifc:00000",
        ordinal=0,
        kind=UnitKind.BIM_MODEL.value,
        route=Route.NATIVE_IFC.value,
    )


def _summary(tmp_path: Path):
    path = tmp_path / "model.ifc"
    path.write_text(MODEL, encoding="latin-1")
    return read_ifc(path)


def test_step_strings_and_arguments() -> None:
    assert decode_string("d''alimentation") == "d'alimentation"
    assert decode_string("\\X2\\00E9\\X0\\t\\X\\E9") == "été"
    assert split_arguments("'a,b',#1,(1,2),IFCLABEL('x'),$") == [
        "'a,b'", "#1", "(1,2)", "IFCLABEL('x')", "$",
    ]


def test_document_numbers_need_a_full_code() -> None:
    from tizertmaint_pipeline.stages.normalizers.ifc import DOCUMENT

    text = "SP0904-6028-S-MODL-25137_001 ref SP0904-6100-S and SP0904-0000-1805-002."
    assert DOCUMENT.findall(text) == ["SP0904-6028-S-MODL-25137", "SP0904-0000-1805-002"]


def test_reader_keeps_identity_and_skips_geometry(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    assert summary.schema == "IFC2X3"
    assert summary.originating_system == "Advance Steel 2024"
    assert summary.organization == "TIZERT"
    assert [(item.entity, item.name) for item in summary.spatial] == [
        ("IFCPROJECT", "6032-ZA-1274"),
        ("IFCSITE", "6032-ZA-1274"),
        ("IFCBUILDINGSTOREY", "Niveau élévation"),
    ]
    assert summary.spatial[0].long_name == "Goulotte d'alimentation"
    assert summary.spatial[2].long_name == "Plancher"
    assert summary.element_counts == {"IFCPLATE": 2, "IFCBEAM": 1, "IFCELEMENTASSEMBLY": 1}
    assert summary.materials == {"S355JR": 2}
    identifiers = {value: (item.kind, item.where) for value, item in summary.identifiers.items()}
    assert identifiers == {
        "SP0904-6032-M-MODL-20274": ("document", "FILE header"),
        "6032-ZA-1274": ("tag", "IFCPROJECT"),
        "6032-ZA-1110": ("tag", "IFCELEMENTASSEMBLY"),
        "SP0904-M023.00-G02-0013": ("document", "IFCPROPERTYSINGLEVALUE Reference"),
    }
    assert summary.identifiers["6032-ZA-1274"].count == 2
    assert summary.identifiers["6032-ZA-1274"].line == 9
    # The coordinates 1250/-6032/1274 must not be mistaken for a tag.
    assert all("1250" not in value for value in summary.identifiers)


def test_normalized_unit_cites_lines_and_exposes_fields(tmp_path: Path) -> None:
    evidence = normalize_ifc(
        _summary(tmp_path), unit=_unit(), plan_id="plan:test", content_hash=HASH
    )
    assert evidence.extraction_method == "native_ifc"
    title = evidence.blocks[0]
    assert title.kind is BlockKind.TITLE
    assert "Advance Steel 2024" in title.text
    texts = [block.text for block in evidence.blocks]
    assert "IFCPROJECT name 6032-ZA-1274; long name Goulotte d'alimentation" in texts
    assert "Equipment tags in the model: 6032-ZA-1274, 6032-ZA-1110" in texts
    tags = next(block for block in evidence.blocks if block.text.startswith("Equipment tags"))
    assert [(loc.kind, loc.line_start) for loc in tags.locators] == [
        (LocatorKind.LINES, 9),
        (LocatorKind.LINES, 16),
    ]
    table = next(block.table for block in evidence.blocks if block.table is not None)
    assert table.grid()[1] == ["IFCPLATE", "2", "PLATE"]
    fields = {(item.field, item.value) for item in evidence.fields}
    assert ("EquipmentTags", "6032-ZA-1274") in fields
    assert ("DocumentCode", "SP0904-M023.00-G02-0013") in fields
    assert {item.method for item in evidence.fields} == {"native"}
    assert evidence.has_content


def test_models_without_data_are_flagged(tmp_path: Path) -> None:
    path = tmp_path / "empty.ifc"
    path.write_text("ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC2X3'));\nENDSEC;\n", "latin-1")
    summary = read_ifc(path)
    assert summary.warnings == ["no DATA section"]
    evidence = normalize_ifc(summary, unit=_unit(), plan_id="p", content_hash=HASH)
    assert [block.kind for block in evidence.blocks] == [BlockKind.TITLE]
