from FastAutoAugment.nas.model_desc import RunMode
from FastAutoAugment.common.common import common_init
from FastAutoAugment.nas.search import search_arch
from FastAutoAugment.nas.model_desc_builder import ModelDescBuilder
from FastAutoAugment.darts.darts_strategy import DartsStrategy

import yaml
import os

if __name__ == '__main__':
    conf = common_init(config_filepath=None,
        defaults_filepath='confs/defaults.yaml', experiment_name='cifar_search')

    conf_ds = conf['dataset']
    conf_search = conf['darts']['search']
    conf_model_desc = conf_search['model_desc']
    logdir = conf['logdir']

    builder = ModelDescBuilder(conf_ds, conf_model_desc, run_mode=RunMode.Search)
    model_desc = builder.get_model_desc()

    strategy = DartsStrategy()
    strategy.apply(model_desc)

    found_model_desc = search_arch(conf, model_desc)

    found_model_yaml = yaml.dump(found_model_desc)
    with open(os.path.join(logdir, 'model_desc.yaml'), 'w') as f:
        f.write(found_model_yaml)