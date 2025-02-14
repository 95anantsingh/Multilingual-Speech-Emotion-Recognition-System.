# TODO: Add whisper
# TODO: Add BYOL-a /BYOL-s

import os
import json
import argparse
import datetime
from tqdm import tqdm
from time import sleep
from copy import deepcopy
from models import CumulativeProbingLinear, CumulativeProbingDense
from models import GE2E, Dense, ProbingModel, ProbingDense
from tqdm.contrib.logging import logging_redirect_tqdm
from utils import DotDict, NoFmtLog, get_logger, log_levels
from datasets import FeatureExtractorDataset, GPUDiskModeClassifierDataset, \
    CPUMemoryModeClassifierDataset, GPUMemoryModeClassifierDataset

import torch
import torchaudio
from torch.utils.data import DataLoader
from torcheval.metrics import MulticlassAccuracy


VERSION = '1.5'

FX_MODELS = ['WAV2VEC2_BASE','WAV2VEC2_LARGE',
        'WAV2VEC2_LARGE_XLSR','WAV2VEC2_LARGE_XLSR300M',
        'HUBERT_BASE', 'HUBERT_LARGE', 
        'WAV2VEC2_ASR_LARGE_960H', 'HUBERT_ASR_LARGE'  ]
        
FX_MODEL_MAP = {'WAV2VEC2_BASE':'WAV2VEC2_BASE','WAV2VEC2_LARGE':'WAV2VEC2_LARGE',
        'WAV2VEC2_LARGE_XLSR':'WAV2VEC2_XLSR53','WAV2VEC2_LARGE_XLSR300M':'WAV2VEC2_XLSR_300M',
        'HUBERT_BASE':'HUBERT_BASE', 'HUBERT_LARGE':'HUBERT_LARGE',
        'WAV2VEC2_ASR_LARGE_960H':'WAV2VEC2_ASR_LARGE_960H', 'HUBERT_ASR_LARGE':'HUBERT_ASR_LARGE'}

CLF_MODELS = ['DENSE','PROBING','PROBING_DENSE', 'CM_PROBING_LINEAR', 'CM_PROBING_DENSE']

DATASETS = ['AESDD','CaFE','EmoDB','EMOVO','IEMOCAP','RAVDESS','ShEMO']


class Trainer:

    def __init__(self, config):

        self.config = config

        # Setup Directories
        self.data_dir = os.path.join(self.config.data_dir, 'Audios', self.config.dataset)
        self.feature_dir = os.path.join(self.config.data_dir, 'Features', self.config.dataset, self.config.fx_model)
        self.history_dir = os.path.join(self.config.history_dir, f'v{VERSION}',self.config.dataset,
            f'FX_{self.config.fx_model}_CLF_{self.config.clf_model}', self.config.run_name)
        self.weights_dir = os.path.join(self.config.weights_dir, f'v{VERSION}',self.config.dataset,
            f'FX_{self.config.fx_model}_CLF_{self.config.clf_model}', self.config.run_name)

        if self.config.extract_mode == 'disk': os.makedirs(self.feature_dir, exist_ok=True)
        os.makedirs(self.history_dir, exist_ok=True)
        os.makedirs(self.weights_dir, exist_ok=True)

        # Setup logger
        log_file = os.path.join(self.history_dir, f'std.log')
        if os.path.isfile(log_file) : os.system(f'rm {log_file}')
        self.logger, self.no_fmt_logger = get_logger(self.config.run_name, self.config.log_level, log_file)
        self.no_fmt_log = NoFmtLog(self.no_fmt_logger)

        # Log configs
        self._print_banner()
    
        # Setup device
        if self.config.device == 'gpu': 
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else: self.device = 'cpu'
        self.logger.info(f'Running on {str(self.device).upper()}')
        self.no_fmt_log()

        # Load data info
        self.dataset_info = DotDict(json.load(open(os.path.join(self.data_dir,'info.json'))))
        
        # Setup models
        self.fx_model = self._get_feature_extractor()
        self.clf_model = self._get_classifier()
        
        # Setup metric
        self.acc_metric = MulticlassAccuracy(device=self.device)
        
        # Setup history
        self.history = DotDict(train_loss=[],train_acc=[],val_loss=[],val_acc=[],test_loss=[],test_acc=[])
        

    def _print_banner(self):
        try: console_width, _ = os.get_terminal_size(0)
        except : console_width = 50

        banner = '\n'+('-'*console_width)+f'\nMultilingual SER System v{VERSION}\n'+ ('-'*console_width) + '\n'
        max_len = max([len(key) for key in self.config.__dict__.keys()])+2
        for arg,val in self.config.__dict__.items():
            banner += ' '.join(arg.split('_')).title() + ' '*(max_len-len(arg))+': '+ str(val) + '\n'
        try: banner += 'Job Id' + ' '*(max_len-len('Job Id'))+': '+ str(os.environ['SLURM_JOB_ID']) + '\n'
        except: pass
        if torch.cuda.is_available() and self.config.device == 'gpu':
            banner += 'GPU' + ' '*(max_len-len('GPU'))+': '+ torch.cuda.get_device_name() + '\n'
            banner += 'GPU Count' + ' '*(max_len-len('GPU Count'))+': '+ str(torch.cuda.device_count()) + '\n'
            banner += 'GPU Memory' + ' '*(max_len-len('GPU Memory'))+': '+ str(round(torch.cuda.get_device_properties(0).total_memory/1024/1024/1024,2)) + ' GB\n'
        banner += f'\nTimestamp : {datetime.datetime.now()}\n'
        self.no_fmt_log(msg=banner)


    def _get_feature_extractor(self):
        # Load Model
        try:
            if self.config.fx_model == 'GE2E':
                model = GE2E()
                checkpoint = torch.load(os.path.join(self.config.weights_dir,'pretrained',self.config.fx_model+'_weights.pt'), 'cpu')
                model.load_state_dict(checkpoint["model_state"])
            else:
                bundle = getattr(torchaudio.pipelines, FX_MODEL_MAP[self.config.fx_model])
                model = bundle.get_model()
        except:
            self.logger.exception(f"Could not load {self.config.fx_model} feature extractor")
            exit(0)
        return model


    def _get_classifier(self):
        
        if self.config.clf_model == 'PROBING': # orself.config.fx_model == 'GE2E'
            model = ProbingModel(self.config.fx_model, len(self.dataset_info.label_map))
        elif self.config.clf_model == 'PROBING_DENSE':
            model = ProbingDense(self.config.fx_model, len(self.dataset_info.label_map), self.device)
        elif self.config.clf_model == 'CM_PROBING_DENSE':
            model = CumulativeProbingDense(self.config.fx_model, len(self.dataset_info.label_map), 1, self.device)
        elif self.config.clf_model == 'CM_PROBING_LINEAR':
            model = CumulativeProbingLinear(self.config.fx_model, len(self.dataset_info.label_map), 1)
        elif self.config.clf_model == 'DENSE':
            model = Dense(self.config.fx_model, len(self.dataset_info.label_map), self.device)
           
        return model


    def _get_fx_dataloaders(self):
        test_dataset = FeatureExtractorDataset(self.config.fx_model, self.data_dir, 'test')
        train_dataset = FeatureExtractorDataset(self.config.fx_model, self.data_dir, 'train')
        val_dataset = FeatureExtractorDataset(self.config.fx_model, self.data_dir, 'validation')
        
        train_batch_size = 32
        test_batch_size = 32
        num_workers = self.config.num_workers

        test_loader = DataLoader(test_dataset, batch_size=test_batch_size, shuffle=False,
            num_workers=num_workers, collate_fn = test_dataset.data_collator)
        train_loader = DataLoader(train_dataset,batch_size=train_batch_size, shuffle=False,
            num_workers=num_workers, collate_fn = train_dataset.data_collator)
        val_loader = DataLoader(val_dataset, batch_size=test_batch_size, shuffle=False,
            num_workers=num_workers, collate_fn = val_dataset.data_collator)

        return DotDict(train=train_loader, test=test_loader, validation=val_loader)

    
    def _get_clf_dataloaders(self):
        
        if self.config.extract_mode=='gpu_disk':
            test_dataset = GPUDiskModeClassifierDataset(self.config.fx_model, self.feature_dir, self.dataset_info, 'test')
            train_dataset = GPUDiskModeClassifierDataset(self.config.fx_model, self.feature_dir, self.dataset_info, 'train')
            val_dataset = GPUDiskModeClassifierDataset(self.config.fx_model, self.feature_dir, self.dataset_info, 'validation')
        elif self.config.extract_mode=='cpu_memory':
            test_dataset = CPUMemoryModeClassifierDataset(self.config.fx_model, self.fx_model, self.data_dir, self.dataset_info, 'test')
            train_dataset = CPUMemoryModeClassifierDataset(self.config.fx_model, self.fx_model, self.data_dir, self.dataset_info, 'train')
            val_dataset = CPUMemoryModeClassifierDataset(self.config.fx_model, self.fx_model, self.data_dir, self.dataset_info, 'validation')
        elif self.config.extract_mode=='gpu_memory':
            test_dataset = GPUMemoryModeClassifierDataset(self.config.fx_model, self.data_dir, self.dataset_info, 'test')
            train_dataset = GPUMemoryModeClassifierDataset(self.config.fx_model, self.data_dir, self.dataset_info, 'train')
            val_dataset = GPUMemoryModeClassifierDataset(self.config.fx_model, self.data_dir, self.dataset_info, 'validation')

        train_batch_size = 32
        test_batch_size = 32
        num_workers = self.config.num_workers

        test_loader = DataLoader(test_dataset, batch_size=test_batch_size, shuffle=False,
            num_workers=num_workers, collate_fn = test_dataset.data_collator)
        train_loader = DataLoader(train_dataset,batch_size=train_batch_size, shuffle=True,
            num_workers=num_workers, collate_fn = train_dataset.data_collator)
        val_loader = DataLoader(val_dataset, batch_size=test_batch_size, shuffle=False,
            num_workers=num_workers, collate_fn = val_dataset.data_collator)

        return DotDict(train=train_loader, test=test_loader, validation=val_loader)


    def _extract_features(self):
        self.fx_model.to(self.device)
        self.fx_model.eval()

        processing_required = False
        processing_lock = os.path.join(self.feature_dir,'processing.lock')
          
        # Check if extraction is running
        while os.path.isfile(processing_lock): 
            self.logger.info('Waiting for other process to finish processing')
            sleep(60)
        else: self.no_fmt_log()
        
        # Check if extraction is required
        for split, dataloader in self.fx_dataloaders.items(): 
            if os.path.isdir(os.path.join(self.feature_dir,split)):
                self.logger.info(f'Feature cache found for {split} split')
            else: 
                self.logger.info(f'Feature cache not found for {split} split')
                processing_required = True
                
        if self.config.purge_cache and processing_required==False:
            os.system(f'rm -rf {self.feature_dir}')
            os.makedirs(self.feature_dir)
            self.logger.info('Feature cache purged')
            processing_required = True

        # Extract features if required
        if processing_required:
            self.no_fmt_log()
            self.logger.info('Extracting Features')
            file = open(processing_lock,'w')
            file.close()
            self.no_fmt_log()
            self.logger.debug('FX processing lock active')
            self.no_fmt_log()

            pbar = tqdm(desc='Extracting Features ', unit=' batch', colour='blue', total= sum([len(loader) for loader in self.fx_dataloaders.values()]))
            with logging_redirect_tqdm(loggers=[self.logger, self.no_fmt_logger]):
                for split, dataloader in self.fx_dataloaders.items():
                    feature_dir = os.path.join(self.feature_dir,split)
                    os.makedirs(feature_dir,exist_ok=True)
                    for batch in dataloader:
                        input = batch[0].to(self.device)
                        with torch.inference_mode():
                            if self.config.fx_model == 'GE2E':
                                outputs = self.fx_model(input)
                            else:
                                outputs, _ = self.fx_model.extract_features(input)
                                outputs = torch.stack([*outputs],dim=1)
                                    
                        for output, file_name in zip(outputs,batch[1]):
                            file_name = file_name.split('/')[-1].split('.')[0] +'.ftr'
                            torch.save(output,os.path.join(feature_dir,file_name))
                        pbar.update(1)

                time_taken = pbar.format_dict['elapsed']
            pbar.close()

            self.no_fmt_log()
            self.logger.info(f'Time Taken: {pbar.format_interval(time_taken)}')
            self.no_fmt_log()

            os.remove(processing_lock)
            self.logger.info('Feature extraction complete')
            self.no_fmt_log()
            self.logger.debug('FX processing lock released')


    def _train(self, dataloader, optimizer, criterion, progress_bar):
        total_loss = 0.0
        self.clf_model.train()
        for batch in dataloader:
            batch = tuple(input.to(self.device) for input in batch)

            optimizer.zero_grad()
            output = self.clf_model(batch[0],batch[1])

            loss = criterion(output, batch[2])
            
            loss.backward()
            optimizer.step()
            
            total_loss += loss.item()
            self.acc_metric.update(output, batch[2])

            progress_bar.update(1)
        
        total_loss = total_loss / len(dataloader)
        accuracy = self.acc_metric.compute().item()*100

        self.acc_metric.reset()
        return total_loss, accuracy

    
    def _test(self, dataloader, criterion, progress_bar):
        total_loss = 0.0
        self.clf_model.eval()
        for batch in dataloader:
            batch = tuple(input.to(self.device) for input in batch)
           
            with torch.inference_mode():
                output = self.clf_model(batch[0],batch[1])
                loss = criterion(output, batch[2])

            total_loss += loss.item()
            self.acc_metric.update(output, batch[2])
            
            progress_bar.update(1)
        
        total_loss = total_loss / len(dataloader)
        accuracy = self.acc_metric.compute().item()*100
    
        self.acc_metric.reset()

        return total_loss, accuracy


    def _gpu_train(self, dataloader, optimizer, criterion, progress_bar):
        total_loss = 0.0
        self.clf_model.train()
        self.fx_model.eval()
        for batch in dataloader:
            batch = tuple(input.to(self.device) for input in batch)
            
            with torch.inference_mode():
                if self.config.fx_model == 'GE2E':
                    features = self.fx_model(batch[0])
                else:
                    features, _ = self.fx_model.extract_features(batch[0])
                    features = torch.stack([*features],dim=1)
            
            features = torch.clone(features)

            optimizer.zero_grad()
            output = self.clf_model(features,batch[1])

            loss = criterion(output, batch[2])
            
            loss.backward()
            optimizer.step()
            
            total_loss += loss.item()
            self.acc_metric.update(output, batch[2])

            progress_bar.update(1)
        
        progress_bar.refresh()

        total_loss = total_loss / len(dataloader)
        accuracy = self.acc_metric.compute().item()*100

        self.acc_metric.reset()
        return total_loss, accuracy


    def _gpu_test(self, dataloader, criterion, progress_bar):
        total_loss = 0.0
        self.clf_model.eval()
        self.fx_model.eval()
        for batch in dataloader:
            batch = tuple(input.to(self.device) for input in batch)
           
            with torch.inference_mode():
                if self.config.fx_model == 'GE2E':
                    features = self.fx_model(batch[0])
                else:
                    features, _ = self.fx_model.extract_features(batch[0])
                    features = torch.stack([*features],dim=1)

                output = self.clf_model(features,batch[1])
                loss = criterion(output, batch[2])

            total_loss += loss.item()
            self.acc_metric.update(output, batch[2])
            
            progress_bar.update(1)
        
        progress_bar.refresh()

        total_loss = total_loss / len(dataloader)
        accuracy = self.acc_metric.compute().item()*100
    
        self.acc_metric.reset()

        return total_loss, accuracy


    def _gpu_probing_train(self, models, dataloader, optimizers, criterion, progress_bar):
        num_layers = len(models)
        total_losses = [0.0]*num_layers
        acc_metrics = []

        for layer in range(num_layers): 
            models[layer].train()
            metric = deepcopy(self.acc_metric)
            acc_metrics.append(metric)

        self.pos_conv = None
        def custom_hook(module, input_, output):  self.pos_conv = output
        try: handle = self.fx_model.encoder.transformer.pos_conv_embed.register_forward_hook(custom_hook)
        except: handle = self.fx_model.model.encoder.transformer.pos_conv_embed.register_forward_hook(custom_hook)

        self.fx_model.eval()
        for batch in dataloader:
            batch = tuple(input.to(self.device) for input in batch)
            
            with torch.inference_mode():
                features, _ = self.fx_model.extract_features(batch[0])
                features = torch.stack([self.pos_conv]+[*features],dim=1)

            features = torch.clone(features)

            for layer in range(num_layers):
                optimizers[layer].zero_grad()
                output = models[layer](features,batch[1],layer)

                loss = criterion(output, batch[2])
                
                loss.backward()
                optimizers[layer].step()
                
                total_losses[layer] += loss.item()
                acc_metrics[layer].update(output, batch[2])

                progress_bar.update(1)
        progress_bar.refresh()

        accuracies = []
        for layer in range(num_layers):
            total_losses[layer] = total_losses[layer] / len(dataloader)
            accuracies.append(acc_metrics[layer].compute().item()*100) 

        handle.remove()

        self.acc_metric.reset()
        return total_losses, accuracies


    def _gpu_probing_test(self, models, dataloader, criterion, progress_bar):
        num_layers = len(models)
        total_losses = [0.0]*num_layers
        acc_metrics = []

        for layer in range(num_layers): 
            models[layer].eval()
            metric = deepcopy(self.acc_metric)
            acc_metrics.append(metric)

        self.pos_conv = None
        def custom_hook(module, input_, output):  self.pos_conv = output
        try: handle = self.fx_model.encoder.transformer.pos_conv_embed.register_forward_hook(custom_hook)
        except: handle = self.fx_model.model.encoder.transformer.pos_conv_embed.register_forward_hook(custom_hook)

        self.fx_model.eval()
        for batch in dataloader:
            batch = tuple(input.to(self.device) for input in batch)
            
            with torch.inference_mode():
                features, _ = self.fx_model.extract_features(batch[0])
                features = torch.stack([self.pos_conv]+[*features],dim=1)

                for layer in range(num_layers):
                    output = models[layer](features,batch[1],layer)
                    loss = criterion(output, batch[2])
                    
                    total_losses[layer] += loss.item()
                    acc_metrics[layer].update(output, batch[2])

                    progress_bar.update(1)
        progress_bar.refresh()

        accuracies = []
        for layer in range(num_layers):
            total_losses[layer] = total_losses[layer] / len(dataloader)
            accuracies.append(acc_metrics[layer].compute().item()*100) 

        handle.remove()

        self.acc_metric.reset()
        return total_losses, accuracies


    def train_pipeline(self):
        
        if self.config.extract_mode=='gpu_disk':
            # Extract Features
            self.fx_dataloaders = self._get_fx_dataloaders()
            self._extract_features()
        
        # Setup classifier dataloaders
        self.clf_dataloaders = self._get_clf_dataloaders()

        # Setup criterion
        criterion = torch.nn.CrossEntropyLoss()

        # Set save paths
        best_model_path = os.path.join(self.weights_dir,'best_state.pt')

        train_pbar = tqdm(desc='Training   ', unit=' batch', colour='#EF5350', total=len(self.clf_dataloaders.train))
        val_pbar   = tqdm(desc='Validation ', unit=' batch', colour='#E0E0E0', total=len(self.clf_dataloaders.validation))
        test_pbar  = tqdm(desc='Testing    ', unit=' batch', colour='#42A5F5', total=len(self.clf_dataloaders.test))
        epoch_pbar = tqdm(desc='Epoch      ', unit=' epoch', colour='#43A047', total=self.config.epochs)

        # Train classifier
        self.clf_model.to(self.device)
        if self.config.extract_mode=='gpu_memory': self.fx_model.to(self.device)
        
        with logging_redirect_tqdm(loggers=[self.logger, self.no_fmt_logger]):
            self.no_fmt_log()
            self.logger.info('Training Classifier')
            self.no_fmt_log()

            if 'PROBING' in self.config.clf_model: #and self.config.fx_model!='GE2E':
                models, optimizers = [], []
                if 'BASE' in self.config.fx_model: num_layers = 13
                elif 'LARGE' in self.config.fx_model:  num_layers = 25
                for layer in range(1,num_layers+1):
                    if 'CM_PROBING' in self.config.clf_model:
                        if 'DENSE' in self.config.clf_model:
                            model = CumulativeProbingDense(self.config.fx_model, len(self.dataset_info.label_map), layer, self.device)
                        elif 'LINEAR' in self.config.clf_model:
                            model = CumulativeProbingLinear(self.config.fx_model, len(self.dataset_info.label_map), layer)
                        model.to(self.device)
                    else: model = deepcopy(self.clf_model)
                    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
                    models.append(model)
                    optimizers.append(optimizer)

                train_pbar.unit = ' step'
                val_pbar.unit = ' step'
                test_pbar.unit = ' step'
                train_pbar.total *= num_layers
                val_pbar.total *= num_layers
                test_pbar.total *= num_layers
                train_pbar.refresh()
                val_pbar.refresh()
                test_pbar.refresh()
                best_model = DotDict(val_acc=[-1]*num_layers, test_acc=[0]*num_layers)
            else:
                optimizer = torch.optim.Adam(self.clf_model.parameters(), lr=0.001)
                best_model = DotDict(val_acc=-1, test_acc=0)
                
            for epoch in range(1,self.config.epochs+1):
                
                if 'PROBING' in self.config.clf_model: #and self.config.fx_model!='GE2E':
                    train_loss, train_acc = self._gpu_probing_train(models, self.clf_dataloaders.train, optimizers, criterion, train_pbar)
                    val_loss, val_acc = self._gpu_probing_test(models, self.clf_dataloaders.validation, criterion, val_pbar)
                    test_loss, test_acc = self._gpu_probing_test(models, self.clf_dataloaders.test, criterion, test_pbar)
                    
                    epoch_pbar.update(1)

                    for layer in range(num_layers):
                        self.logger.info('Epoch: %s  |  Layer: %02d  |  Train Loss: %.3f | Train Acc: %.2f  |  Val Loss: %.3f | Val Acc: %.2f  |  Test Loss: %.3f | Tes Acc: %.2f' \
                                    %(epoch, layer+1, train_loss[layer], train_acc[layer], val_loss[layer], val_acc[layer], test_loss[layer], test_acc[layer]))
                        if val_acc[layer] > best_model.val_acc[layer]:
                            best_model.val_acc[layer] = val_acc[layer]
                            best_model.test_acc[layer] = test_acc[layer]
                    self.no_fmt_log()                   

                else:
                    if self.config.extract_mode=='gpu_memory':
                        train_loss, train_acc = self._gpu_train(self.clf_dataloaders.train, optimizer, criterion, train_pbar)
                        val_loss, val_acc = self._gpu_test(self.clf_dataloaders.validation, criterion, val_pbar)
                        test_loss, test_acc = self._gpu_test(self.clf_dataloaders.test, criterion, test_pbar)
                    elif self.config.extract_mode=='cpu_memory' or self.config.extract_mode=='gpu_disk':
                        train_loss, train_acc = self._train(self.clf_dataloaders.train, optimizer, criterion, train_pbar)
                        val_loss, val_acc = self._test(self.clf_dataloaders.validation, criterion, val_pbar)
                        test_loss, test_acc = self._test(self.clf_dataloaders.test, criterion, test_pbar)

                    epoch_pbar.update(1)
                    self.logger.info('Epoch: %s  |  Train Loss: %.3f | Train Acc: %.2f  |  Val Loss: %.3f | Val Acc: %.2f  |  Test Loss: %.3f | Tes Acc: %.2f' \
                                    %(epoch, train_loss, train_acc, val_loss, val_acc, test_loss, test_acc))

                    if val_acc > best_model.val_acc:
                        best_model.val_acc = val_acc
                        best_model.test_acc = test_acc
                        state = dict(
                            epoch=epoch,
                            val_acc=val_acc,
                            test_acc=test_acc,
                            model_state=self.clf_model.state_dict(),
                            optimizer_state=optimizer.state_dict())
                        torch.save(state,best_model_path)
                        self.logger.debug(f'Best state saved | Val Acc {val_acc} | Test Acc {test_acc}')
                        self.no_fmt_log(level='debug')

                self.history.train_loss.append(train_loss)
                self.history.train_acc.append(train_acc)
                self.history.val_loss.append(val_loss)
                self.history.val_acc.append(val_acc)
                self.history.test_loss.append(test_loss)
                self.history.test_acc.append(test_acc)

                train_pbar.reset()
                val_pbar.reset()
                test_pbar.reset()

            train_pbar.update(train_pbar.total)
            val_pbar.update(val_pbar.total)
            test_pbar.update(test_pbar.total)
            self.no_fmt_log()
            time_taken = epoch_pbar.format_dict['elapsed']
            train_pbar.close()
            val_pbar.close()
            test_pbar.close()
            epoch_pbar.close()

        self.no_fmt_log()
        self.logger.info(f'Time Taken: {epoch_pbar.format_interval(time_taken)}')
        self.no_fmt_log()

        if 'PROBING' in self.config.clf_model: #and self.config.fx_model!='GE2E':
            
            for layer in range(num_layers):
                self.logger.info('Layer: %02d  |  Best Val Acc: %.2f  |  Best Test Acc: %.2f' \
                                    %(layer+1, best_model.val_acc[layer], best_model.test_acc[layer]))
            self.no_fmt_log()

            # Save last state
            last_state_path = os.path.join(self.weights_dir, 'last_states.pt')
            states=[]
            for layer in range(num_layers): 
                state = dict(
                    epoch=epoch,
                    val_acc=val_acc[layer],
                    test_acc=test_acc[layer],
                    model_state=models[layer].state_dict(),
                    optimizer_state=optimizers[layer].state_dict())
                states.append(state)
            torch.save(states,last_state_path)
            self.logger.debug(f'Last state saved')

            if 'CM_PROBING' in  self.config.clf_model: 
                mixing_weights=[]
                for layer in range(num_layers):
                    mixing_weights.append(models[layer].prob_weights.squeeze().tolist())
                p_mx_w = '\n'.join([str(w) for w in mixing_weights])
                self.logger.info(f'Mixing Weights (softmax): \n{p_mx_w}\n')
                mx_weight_path = os.path.join(self.history_dir, 'mx_weights.pt')
                torch.save(mixing_weights,mx_weight_path)
        else:
            self.logger.info(f'Best Val Acc : {best_model.val_acc} | Best Test Acc : {best_model.test_acc}')
            self.no_fmt_log()

            # Agg weights
            if self.config.fx_model != 'GE2E' and self.config.clf_model == 'DENSE':
                agg_weights = ' '.join([str(weight[0]) for weight in self.clf_model.aggr.state_dict()['weight'][0].detach().cpu().tolist()])
                self.no_fmt_log()
                self.logger.info(f'Agg. Weights : \n{agg_weights}\n')
                agg_weight_path = os.path.join(self.history_dir, 'agg_weights.pt')
                torch.save(agg_weights,agg_weight_path)
            

            # Save last state
            last_state_path = os.path.join(self.weights_dir, 'last_state.pt')
            state = dict(
                epoch=epoch,
                val_acc=val_acc,
                test_acc=test_acc,
                model_state=self.clf_model.state_dict(),
                optimizer_state=optimizer.state_dict())
            torch.save(state,last_state_path)
            self.logger.debug(f'Last state saved | Val Acc {val_acc} | Test Acc {test_acc}')

        # Save history
        history_path = os.path.join(self.history_dir, 'history.pt')
        torch.save(dict(self.history),history_path)
        self.no_fmt_log()
        self.logger.info(f'History saved ({history_path})')

        return self.history
        

def get_args():
    """Parse input arguments"""
    parser = argparse.ArgumentParser(f'Multilingual SER System v{VERSION}')

    parser.add_argument("-r","--run_name", metavar="<str>", default="test", type=str,
                        help='Run Name') 
    parser.add_argument("-fm","--fx_model", metavar="<str>", default="GE2E", type=str,
                        choices=FX_MODELS, help=str(FX_MODELS))
    parser.add_argument("-cm","--clf_model", metavar="<str>", default="PROBING", type=str,
                        choices=CLF_MODELS, help=str(CLF_MODELS))
    parser.add_argument("-em","--extract_mode", metavar="<str>", default="cpu_memory", type=str,
                        choices=['gpu_disk','gpu_memory','cpu_memory'], help='GPU Disk mode will extract features on GPU and will save the features \
                            to disk and then training will continue using disk cache, GPU Memory mode will extract features and train the model both on GPU,\
                            CPU Memory mode will extract features on CPU and run while training on GPU') 
    parser.add_argument("-d","--dataset", metavar="<str>", default="EmoDB", type=str,
                        choices=DATASETS, help=str(DATASETS))


    parser.add_argument("-dv","--device", metavar="<str>", default="gpu", type=str,
                        choices=['cpu','gpu'], help='Device to run on') 
    parser.add_argument("-e","--epochs", metavar="<int>", default=20, type=int,
                        help='Number of training epochs')
    parser.add_argument("-nw","--num_workers", metavar="<int>", default=2, type=int,
                        help='Number of dataloader workers')


    parser.add_argument("-dd","--data_dir", metavar="<dir>", default="./data", type=str,
                        help='Data directory')     
    parser.add_argument("-hd","--history_dir", metavar="<dir>", default="./history", type=str,
                        help='History directory')     
    parser.add_argument("-wd","--weights_dir", metavar="<dir>", default="./weights", type=str,
                        help='Weights directory')


    parser.add_argument("-ll","--log_level", metavar="<str>", default="info", type=str,
                        choices=list(log_levels.keys()), help=str(list(log_levels.keys())))
    parser.add_argument("-pc","--purge_cache", action="store_true", default=False,
                        help='Purge cached features and extract them again')  
    parser.add_argument("-jn","--job_name", metavar='<str>', default='Manual_Run', type=str,
                        help='SLURM Job Name for logging')                                            

    return parser.parse_args()


if __name__ == '__main__':
    args = get_args()
        
    # Test
    if args.run_name =='test':

        args.dataset = 'CaFE'
        # args.fx_model = 'HUBERT_BASE'
        #' ['WAV2VEC2_BASE','WAV2VEC2_LARGE','WAV2VEC2_LARGE_XLSR','WAV2VEC2_LARGE_XLSR300M','HUBERT_BASE','HUBERT_LARGE','WAV2VEC2_ASR_LARGE_960H', 'HUBERT_ASR_LARGE']
        args.fx_model = 'WAV2VEC2_ASR_LARGE_960H'
        args.clf_model = 'CM_PROBING_DENSE'
        args.extract_mode ='gpu_memory'
        args.history_dir = './test/history'
        args.weights_dir = './test/weights'
        args.log_level = 'debug'
        args.num_workers = 3

        args.epochs = 2

        os.system('rm -rf ./test')
        os.system('mkdir test')
        os.system('mkdir test/weights')
        os.system('ln -s /scratch/as14229/Projects/Multilingual-Speech-Emotion-Recognition-System/weights/pretrained test/weights/pretrained')

    # Train Classifier
    trainer = Trainer(args)
    trainer.train_pipeline()