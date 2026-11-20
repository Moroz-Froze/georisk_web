"""One process per calculation: GDAL state and cancellation stay isolated."""
import importlib
from pathlib import Path
import sys
import traceback
import time
from .exports import prepare_result
from .inputs import config_for
from .storage import read_json, write_json


def run(folder):
    request = read_json(folder/'request.json')
    root = folder.parents[1]
    last = [0.0]
    def notify(percent, message):
        now = time.monotonic()
        if now-last[0] < .2:
            return
        last[0] = now
        write_json(folder/'progress.json', {'progress': min(98, max(0, round(percent))), 'message': message})
    notify(1, 'Проверка геометрии и входных данных…')
    config = config_for(request, root, folder/'result')
    workflow = importlib.import_module(request['hazard']+'_scenario.workflow')
    options = {}
    if request['hazard'] == 'avalanche':
        from .avalanche_batches import simulate_batched
        options['simulator'] = simulate_batched
    result = workflow.run_workflow(config, notify, lambda: (folder/'cancel.flag').exists(), **options)
    notify(98, 'Подготовка карт для браузера…')
    prepare_result(folder.name, folder, request['hazard'], result)


if __name__ == '__main__':
    directory = Path(sys.argv[1])
    try:
        run(directory)
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else 'Не удалось выполнить расчёт. Сообщите идентификатор расчёта администратору.'
        if str(directory.parents[1]) in message or str(Path.cwd()) in message:
            message = 'Не удалось обработать исходные данные. Проверьте файлы и параметры расчёта.'
        write_json(directory/'error.json', {'message': message[:1000], 'code': getattr(error, 'code', None)})
        traceback.print_exc()
        raise SystemExit(1)
