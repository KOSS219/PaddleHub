# Copyright (c) 2019  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

import paddle
import paddle.fluid as fluid

from paddle_hub.tools.logger import logger


def optimizer_config_for_strategy(strategy, parameters, data_processor,
                                  dev_count):
    # basic configuration
    learning_rate = 1e-4
    optimizer = fluid.optimizer.Adam(learning_rate)
    regularizer = fluid.regularizer.L2DecayRegularizer(
        regularization_coeff=1e-4)

    return optimizer


def _finetune_model(task,
                    data_processor,
                    feed_list,
                    config=None,
                    eval_model=False):
    main_program = task.main_program()
    startup_program = task.startup_program()
    loss = task.variable("loss")
    accuracy = task.variable("accuracy")

    epoch = config.num_epoch
    batch_size = config.batch_size
    learning_rate = config.learning_rate
    use_cuda = config.use_cuda
    batch_size = config.batch_size
    strategy = config.strategy
    with_memory_optimization = config.with_memory_optimization
    checkpoint_dir = config.checkpoint_dir

    with fluid.program_guard(main_program, startup_program):

        if use_cuda:
            place = fluid.CUDAPlace(0)
            dev_count = fluid.core.get_cuda_device_count()
        else:
            place = fluid.CPUPlace()
            dev_count = int(
                os.environ.get('CPU_NUM', multiprocessing.cpu_count()))

        optimizer = optimizer_config_for_strategy(
            strategy=strategy,
            parameters=None,
            data_processor=data_processor,
            dev_count=dev_count)
        data_feeder = fluid.DataFeeder(feed_list=feed_list, place=place)
        exe = fluid.Executor(place=place)
        optimizer.minimize(loss)

        if with_memory_optimization:
            logger.info("Memory optimize start")
            fluid.memory_optimize(
                input_program=fluid.default_main_program(),
                skip_opt_set=[
                    # skip task graph variable memory optimization
                    loss.name,
                    accuracy.name
                ])
            logger.info("Memory optimize end")

        # initilize all parameters
        exe.run(fluid.default_startup_program())
        step = 0
        logger.info("Finetune start")
        train_time_begin = time.time()
        for index in range(epoch):
            train_reader = paddle.batch(
                data_processor.data_generator(phase='train'),
                batch_size=batch_size)
            size = accuracy_sum = loss_sum = 0
            for batch in train_reader():
                loss_v, accuracy_v = exe.run(
                    feed=data_feeder.feed(batch),
                    fetch_list=[loss.name, accuracy.name])
                step += 1
                size += len(batch)
                accuracy_sum += accuracy_v * len(batch)
                loss_sum += loss_v * len(batch)

                if step % config.log_interval == 0:
                    train_time_used = time.time() - train_time_begin
                    perf = train_time_used / config.log_interval
                    train_time_begin = time.time()
                    logger.info(
                        "step %d: loss=%.5f acc=%.5f [step/sec: %.2f]" %
                        (step, loss_sum / size, accuracy_sum / size, perf))
                    size = accuracy_sum = loss_sum = 0

                if step % config.save_ckpt_interval == 0:
                    model_save_dir = os.path.join(
                        checkpoint_dir, "model_parameters_in_step%d" % step)
                    fluid.io.save_persistables(exe, dirname=model_save_dir)

                if eval_model and step % config.eval_interval == 0:
                    eval(task, data_processor, feed_list, config)
        # eval before end
        if eval_model:
            eval(task, data_processor, feed_list, config)
        logger.info("Finetune end")


def save_model_and_checkpoint(task, save_dir):
    pass


def finetune_and_eval(
        task,
        data_processor,
        feed_list,
        config=None,
):
    _finetune_model(task, data_processor, feed_list, config, eval_model=True)


def finetune(task, data_processor, feed_list, config=None):
    _finetune_model(task, data_processor, feed_list, config, eval_model=False)


def eval(task, data_processor, feed_list, config=None):
    inference_program = task.inference_program()
    main_program = task.main_program()
    loss = task.variable("loss")
    accuracy = task.variable("accuracy")
    use_cuda = config.use_cuda
    batch_size = config.batch_size
    logger.info("[Evaluation] start")
    with fluid.program_guard(inference_program):
        place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
        data_feeder = fluid.DataFeeder(feed_list=feed_list, place=place)
        exe = fluid.Executor(place=place)
        size = accuracy_sum = loss_sum = 0
        test_reader = paddle.batch(
            data_processor.data_generator(phase='test'), batch_size=batch_size)
        eval_time_begin = time.time()
        for index, batch in enumerate(test_reader()):
            loss_v, accuracy_v, = exe.run(
                feed=data_feeder.feed(batch), fetch_list=[loss, accuracy.name])
            size += len(batch)
            accuracy_sum += accuracy_v * len(batch)
            loss_sum += loss_v * len(batch)
        eval_time_used = time.time() - eval_time_begin
        perf = eval_time_used / index
    logger.info("[Evaluation] loss=%.5f acc=%.5f [step/sec: %.2f]" %
                (loss_sum / size, accuracy_sum / size, perf))