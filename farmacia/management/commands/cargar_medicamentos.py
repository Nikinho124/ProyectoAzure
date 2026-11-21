from django.core.management.base import BaseCommand

from farmacia.models import Medicamento


class Command(BaseCommand):
    help = "Carga o actualiza 25 medicamentos de prueba"

    def handle(self, *args, **options):
        medicamentos = [
            ("MED-001", "Paracetamol 500 mg", "Analgesico y antipiretico de venta libre", 1500, False, 5),
            ("MED-002", "Ibuprofeno 400 mg", "Antiinflamatorio y analgesico de venta libre", 2200, False, 5),
            ("MED-003", "Loratadina 10 mg", "Antihistaminico para alivio de alergias", 2800, False, 4),
            ("MED-004", "Omeprazol 20 mg", "Inhibidor de bomba de protones para acidez", 3200, False, 6),
            ("MED-005", "Cetirizina 10 mg", "Antihistaminico para sintomas alergicos", 2600, False, 5),
            ("MED-006", "Ácido acetilsalicílico 100 mg", "Antiagregante y analgesico", 1800, False, 4),
            ("MED-007", "Diclofenaco 50 mg", "Antiinflamatorio no esteroideo", 3400, False, 5),
            ("MED-008", "Naproxeno 250 mg", "Antiinflamatorio y analgesico", 3000, False, 5),
            ("MED-009", "Salbutamol 100 mcg", "Broncodilatador inhalado", 5900, False, 3),
            ("MED-010", "Dextrometorfano jarabe", "Antitusivo para tos seca", 3900, False, 4),
            ("MED-011", "Clotrimazol crema 1 %", "Antifungico de uso topico", 4500, False, 4),
            ("MED-012", "Vitamina C 500 mg", "Suplemento vitaminico", 2400, False, 7),
            ("MED-013", "Amoxicilina 500 mg", "Antibiotico betalactamico", 4200, True, 6),
            ("MED-014", "Losartan 50 mg", "Antihipertensivo bloqueador de receptores", 3800, True, 8),
            ("MED-015", "Metformina 850 mg", "Antidiabetico oral para diabetes tipo 2", 3500, True, 10),
            ("MED-016", "Atorvastatina 20 mg", "Tratamiento para el colesterol elevado", 4800, True, 8),
            ("MED-017", "Enalapril 10 mg", "Inhibidor ECA para hipertension", 3000, True, 7),
            ("MED-018", "Amlodipino 5 mg", "Bloqueador de canales de calcio", 3300, True, 7),
            ("MED-019", "Sertralina 50 mg", "Antidepresivo inhibidor selectivo de serotonina", 5200, True, 6),
            ("MED-020", "Azitromicina 500 mg", "Antibiotico macrólido", 6500, True, 5),
            ("MED-021", "Levotiroxina 100 mcg", "Hormona tiroidea para hipotiroidismo", 4100, True, 7),
            ("MED-022", "Clonazepam 2 mg", "Ansiolítico de control especial", 4700, True, 3),
            ("MED-023", "Alprazolam 0.5 mg", "Ansiolítico de control especial", 4300, True, 3),
            ("MED-024", "Tramadol 50 mg", "Analgesico opioide de control especial", 5600, True, 4),
            ("MED-025", "Zolpidem 10 mg", "Hipnótico de control especial", 6200, True, 4),
        ]

        creados = 0
        actualizados = 0

        for codigo, nombre, descripcion, precio, requiere_receta, stock_minimo in medicamentos:
            medicamento, creado = Medicamento.objects.update_or_create(
                codigo=codigo,
                defaults={
                    "nombre": nombre,
                    "descripcion": descripcion,
                    "precio": precio,
                    "requiere_receta": requiere_receta,
                    "stock_minimo": stock_minimo,
                    "activo": True,
                },
            )
            if creado:
                creados += 1
            else:
                actualizados += 1

        self.stdout.write(self.style.SUCCESS(f"Listo: {creados} creados, {actualizados} actualizados."))
