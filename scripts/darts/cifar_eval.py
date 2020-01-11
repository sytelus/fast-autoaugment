from FastAutoAugment.darts.darts_micro_builder import DartsMicroBuilder
from FastAutoAugment.common.common import common_init
from FastAutoAugment.nas.evaluate import eval_arch

if __name__ == '__main__':
    conf = common_init(config_filepath='confs/darts_cifar.yaml',
                       experiment_name='darts_cifar_eval')

    conf_eval = conf['nas']['eval']

    # evaluate architecture using eval settings
    eval_arch(conf_eval, micro_builder=DartsMicroBuilder())

    exit(0)

