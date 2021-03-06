import torch
import torch.nn.functional as F
from torch import nn


class Head(nn.Module):

    def __init__(self, mode, controller_size, key_size,device):
        super().__init__()
        
        self.device = device
        
        self.mode = mode
        self.key_size = key_size
        
        # all the fc layers to produce scalars for memory addressing
        self.key_fc = nn.Linear(controller_size, key_size).to(device)
        self.key_strength_fc = nn.Linear(controller_size, 1).to(device)

        # these five fc layers cannot be in controller class
        # since each head has its own parameters and scalars
        self.interpolation_gate_fc = nn.Linear(controller_size, 1).to(device)
        self.shift_weighting_fc = nn.Linear(controller_size, 3).to(device)
        self.sharpen_factor_fc = nn.Linear(controller_size, 1).to(device)
        # --(optional : for separation of add and erase mechanism)
        #self.erase_weight_fc = nn.Linear(controller_size, key_size)

        # fc layer to produce write data. data vector length=key_size
        self.write_data_fc = nn.Linear(controller_size, key_size).to(device)
        self.reset()

    def forward(self, controller_state, prev_weights, memory, data=None):
        """Accept previous state (weights and memory) and controller state,
        produce attention weights for current read or write operation.
        Weights are produced by content-based and location-based addressing.
        Refer *Figure 2* in the paper to see how weights are produced.
        The head returns current weights useful for next time step, while
        it reads from or writes to ``memory`` based on its mode, using the
        ``data`` vector. ``data`` is filled and returned for read mode,
        returned as is for write mode.
        Refer *Section 3.1* for read mode and *Section 3.2* for write mode.
        Parameters
        ----------
        controller_state : torch.Tensor
            Long-term state of the controller.
            ``(batch_size, controller_size)``
        prev_weights : torch.Tensor
            Attention weights from previous time step.
            ``(batch_size, memory_units)``
        memory : ntm_modules.NTMMemory
            Memory Instance. Read write operations will be performed in place.
        data : torch.Tensor
            Depending upon the mode, this data vector will be used by memory.
            ``(batch_size, memory_unit_size)``
        Returns
        -------
        current_weights, data : torch.Tensor, torch.Tensor
            Current weights and data (filled in read operation else as it is).
            ``(batch_size, memory_units), (batch_size, memory_unit_size)``
        """

        # all these are marked as "controller outputs" in Figure 2
        key = self.key_fc(controller_state)
        # print(f"key size: {key.shape}")
        b = F.softplus(self.key_strength_fc(controller_state))
        g = torch.sigmoid(self.interpolation_gate_fc(controller_state))
        s = F.softmax(self.shift_weighting_fc(controller_state),dim=1)
        # here the sharpening factor is less than 1 whereas as required in the
        # paper it should be greater than 1. hence adding 1.
        y = 1 + F.softplus(self.sharpen_factor_fc(controller_state))
        #e = F.sigmoid(self.erase_weight_fc(controller_state))  # erase vector
        a = self.write_data_fc(controller_state)  # add vector

        content_weights = memory.content_addressing(key, b)
        # print(f"Content weights: {content_weights.shape}")
        # location-based addressing - interpolate, shift, sharpen
        interpolated_weights = g * content_weights + (1 - g) * prev_weights
        # print(f"interpolated weights: {interpolated_weights.shape}")
        shifted_weights = self._circular_conv1d(interpolated_weights, s, self.device)
        # print(f"shifted weights: {shifted_weights.shape}")
        # the softmax introduces the exp of the argument which isn't there in
        # the paper. there it's just a simple normalization of the arguments.
        current_weights = shifted_weights ** y
        # current_weights = F.softmax(shifted_weights ** y)
        current_weights = torch.div(current_weights, torch.sum(
            current_weights, dim=1).view(-1, 1) + 1e-16)

        if self.mode == 'r':
#             print("Read Head")
            data = memory.read(current_weights)
        elif self.mode == 'w':
#             print("Write Head")
            #memory.write(current_weights, a, e)
            memory.write(current_weights, a)
        else:
            raise ValueError("mode must be read ('r') or write('w')")
        return current_weights, data


    
    @staticmethod
    def _circular_conv1d(in_tensor, weights,device):
        # pad left with elements from right, and vice-versa
        # print("In circular convolution")
        # print(f"input tesnsor: {in_tensor.shape}")
        # print(f"input weights: {weights.shape}")
        batch_size = weights.size(0)
        pad = int((weights.size(1) - 1) / 2)

        # in_tensor = torch.cat(
        #     [in_tensor[:, -pad:], in_tensor, in_tensor[:, :pad]], dim=1)
        # print(f"input tesnsor: {in_tensor.shape}")
        # print(f"input tesnsor to convolve: {in_tensor.view(batch_size, 1, -1).shape}")
        # print(f"weight tesnsor to convolve: {weights.view(batch_size, 1, -1).shape}")

        # out_tensor = F.conv1d(in_tensor.view(batch_size, 1, -1),
        #                       weights.view(batch_size, 1, -1))
        # print(f"output tesnsor: {out_tensor.shape}")
        # out_tensor = out_tensor.view(batch_size, -1)
        # print(f"output tesnsor reshaped: {out_tensor.shape}")

        def _convolve(w, s):
            """Circular convolution implementation."""
            assert s.size(0) == 3
            t = torch.cat([w[-1:], w, w[:1]])
            c = F.conv1d(t.view(1, 1, -1), s.view(1, 1, -1)).view(-1)
            return c
        
        result = torch.zeros(in_tensor.size(),device=device)
        for b in range(batch_size):
            result[b] = _convolve(in_tensor[b], weights[b])
        return result
    


    def reset(self):
        nn.init.xavier_uniform_(self.key_strength_fc.weight, gain=1.4)
        nn.init.xavier_uniform_(self.interpolation_gate_fc.weight, gain=1.4)
        nn.init.xavier_uniform_(self.shift_weighting_fc.weight, gain=1.4)
        nn.init.xavier_uniform_(self.sharpen_factor_fc.weight, gain=1.4)
        nn.init.xavier_uniform_(self.write_data_fc.weight, gain=1.4)
        #nn.init.xavier_uniform_(self.erase_weight_fc.weight, gain=1.4)

        # nn.init.kaiming_uniform_(self.key_strength_fc.weight)
        # nn.init.kaiming_uniform_(self.interpolation_gate_fc.weight)
        # nn.init.kaiming_uniform_(self.shift_weighting_fc.weight)
        # nn.init.kaiming_uniform_(self.sharpen_factor_fc.weight)
        # nn.init.kaiming_uniform_(self.write_data_fc.weight)
        # nn.init.kaiming_uniform_(self.erase_weight_fc.weight)

        nn.init.normal_(self.key_fc.bias, std=0.01)
        nn.init.normal_(self.key_strength_fc.bias, std=0.01)
        nn.init.normal_(self.interpolation_gate_fc.bias, std=0.01)
        nn.init.normal_(self.shift_weighting_fc.bias, std=0.01)
        nn.init.normal_(self.sharpen_factor_fc.bias, std=0.01)
        nn.init.normal_(self.write_data_fc.bias, std=0.01)
        #nn.init.normal_(self.erase_weight_fc.bias, std=0.01)